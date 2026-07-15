"""Unit tests for the app-agnostic filter matcher (``common/filter_matching``).

These pin the DISTR-855 contract: a filter token is an anchored **regex** by
default (the SDR / ``object_filter`` shape) and an **exact literal** only when
``exact=True`` (dropdown-id selection). The headline regression is the Looker
#131 class — a regex filter that exact-equality matching silently dropped, so
the whole scope leaked. Here that filter must *include* its matches and the
matcher must *not* fall back to exact equality.
"""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from application_sdk.common import FilterPattern, filter_matches
from application_sdk.common.filter_matching import _to_patterns


class TestRegexDefault:
    """Default semantics: tokens are regexes, matched fully-anchored."""

    def test_regex_include_matches_by_pattern_not_equality(self):
        # The Looker #131 class: a regex filter must include everything that
        # matches it, not only a candidate literally equal to the pattern.
        pattern = FilterPattern.from_filters(include_filter="^prod_.*$")
        assert pattern.matches("prod_sales") is True
        assert pattern.matches("prod_") is True
        assert pattern.matches("staging_sales") is False
        # ...and the pattern string itself is not treated as a literal id.
        assert pattern.matches("^prod_.*$") is False

    def test_unanchored_token_is_full_matched(self):
        # fullmatch semantics: a token must match the ENTIRE candidate, so
        # ``public.*`` scopes prefixes but does not leak substrings.
        pattern = FilterPattern.from_filters(include_filter="public.*")
        assert pattern.matches("public_events") is True
        assert pattern.matches("x_public_events") is False

    def test_list_of_patterns_is_or(self):
        pattern = FilterPattern.from_filters(include_filter=["^a.*$", "^b.*$"])
        assert pattern.matches("apple") is True
        assert pattern.matches("banana") is True
        assert pattern.matches("cherry") is False


class TestExactMode:
    """``exact=True`` escapes each token so regex metacharacters are literal."""

    def test_exact_literal_equality(self):
        assert filter_matches("my.model", include_filter=["my.model"], exact=True)
        # the '.' must be literal, not "any char"
        assert not filter_matches("myXmodel", include_filter=["my.model"], exact=True)

    def test_regex_metachars_are_escaped_in_exact_mode(self):
        # a dropdown id that happens to contain regex chars still matches exactly
        assert filter_matches("a+b(c)", include_filter="a+b(c)", exact=True)
        assert not filter_matches("aaab", include_filter="a+b(c)", exact=True)


class TestIncludeExcludePrecedence:
    def test_empty_include_matches_all(self):
        pattern = FilterPattern.from_filters(include_filter=None)
        assert pattern.matches("anything") is True
        assert pattern.matches("") is True

    def test_empty_exclude_excludes_nothing(self):
        pattern = FilterPattern.from_filters(include_filter="^keep.*$")
        assert pattern.matches("keep_me") is True

    def test_exclude_wins_over_include(self):
        pattern = FilterPattern.from_filters(
            include_filter="^tbl_.*$", exclude_filter="^tbl_tmp_.*$"
        )
        assert pattern.matches("tbl_orders") is True
        assert pattern.matches("tbl_tmp_scratch") is False

    def test_exclude_only(self):
        # no include ⇒ match-all, minus the excluded set
        pattern = FilterPattern.from_filters(exclude_filter=["^_internal$"])
        assert pattern.matches("public_table") is True
        assert pattern.matches("_internal") is False

    def test_none_candidate_never_matches(self):
        assert FilterPattern.from_filters().matches(None) is False  # type: ignore[arg-type]


class TestInputShapes:
    """Accept str / list / dict / JSON-string, reusing the SQL normalizers."""

    def test_hierarchical_dict_uses_sql_normalizer(self):
        # {"^db$": ["^schema$"]} → ^db\.schema$ (anchored), matched on "db.schema"
        pattern = FilterPattern.from_filters(include_filter={"^sales$": ["^public$"]})
        assert pattern.matches("sales.public") is True
        assert pattern.matches("sales.private") is False
        assert pattern.matches("other.public") is False

    def test_dict_star_value_matches_all_schemas(self):
        pattern = FilterPattern.from_filters(include_filter={"^sales$": "*"})
        assert pattern.matches("sales.anything") is True
        assert pattern.matches("other.anything") is False

    def test_json_string_list_is_parsed(self):
        pattern = FilterPattern.from_filters(include_filter='["^a.*$", "^b.*$"]')
        assert pattern.matches("apple") is True
        assert pattern.matches("cherry") is False

    def test_json_string_dict_is_parsed(self):
        pattern = FilterPattern.from_filters(include_filter='{"^sales$": ["^public$"]}')
        assert pattern.matches("sales.public") is True
        assert pattern.matches("sales.private") is False

    def test_empty_and_blank_inputs_are_no_constraint(self):
        for empty in ("", "   ", None, [], {}):
            assert FilterPattern.from_filters(include_filter=empty).matches("x") is True

    def test_bare_regex_string_not_mistaken_for_json(self):
        # a raw regex is not JSON; it must survive as a single pattern token
        assert _to_patterns("^prod_.*$", exact=False) == ["^prod_.*$"]


class TestCompileAndFlags:
    def test_invalid_regex_raises_valueerror(self):
        # unbalanced group — a clear ValueError beats a cryptic re.error
        import pytest

        with pytest.raises(ValueError, match="Invalid filter pattern"):
            FilterPattern.from_filters(include_filter="^(unclosed")

    def test_ignorecase_flag_applies(self):
        pattern = FilterPattern.from_filters(
            include_filter="^prod_.*$", flags=re.IGNORECASE
        )
        assert pattern.matches("PROD_SALES") is True


class TestProperties:
    @given(
        st.text(
            # realistic id characters — filter tokens are edge-stripped for config
            # hygiene, so exclude leading/trailing whitespace/control chars.
            alphabet=st.characters(
                whitelist_categories=("Ll", "Lu", "Nd", "Pc", "Pd", "Po")
            ),
            min_size=1,
            max_size=40,
        ).filter(lambda s: s == s.strip())
    )
    def test_escaped_exact_token_always_matches_itself(self, token: str):
        # For any realistic token, exact-mode matching against itself is reflexive
        # and never raises (re.escape makes every token a valid literal pattern).
        assert filter_matches(token, include_filter=[token], exact=True) is True

    @given(
        st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
                min_size=1,
                max_size=12,
            ),
            min_size=1,
            max_size=5,
        )
    )
    def test_exact_include_list_is_membership(self, ids: list[str]):
        pattern = FilterPattern.from_filters(include_filter=ids, exact=True)
        for i in ids:
            assert pattern.matches(i) is True
        assert pattern.matches("".join(ids) + "_zzz_not_present") is False


class TestReviewRegressions:
    """Regressions for the #2608 review findings (empty JSON sentinels, malformed
    dict scope-leak, exact-mode JSON reinterpretation, non-str candidates)."""

    # #1 — JSON-string empty sentinels must resolve to match-all, not a literal.
    def test_empty_json_object_string_matches_all(self):
        p = FilterPattern.from_filters(include_filter="{}")
        assert p.matches("sales.public") is True
        assert p.matches("anything") is True

    def test_empty_json_array_string_matches_all_and_no_crash(self):
        # "[]" previously compiled to the regex "[]" → ValueError; must be match-all.
        p = FilterPattern.from_filters(include_filter="[]")
        assert p.matches("anything") is True

    def test_empty_json_sentinels_as_exclude_exclude_nothing(self):
        p = FilterPattern.from_filters(include_filter="^keep.*$", exclude_filter="{}")
        assert p.matches("keep_me") is True

    # #2 — malformed dict (scalar-string schema value) must raise, not leak-all.
    def test_scalar_schema_value_dict_raises(self):
        import pytest

        with pytest.raises(ValueError, match="produced no patterns"):
            FilterPattern.from_filters(include_filter={"^sales$": "^public$"})

    def test_scalar_schema_value_via_json_string_raises(self):
        import pytest

        with pytest.raises(ValueError, match="produced no patterns"):
            FilterPattern.from_filters(include_filter='{"^sales$": "^public$"}')

    # #3 — exact=True must treat a JSON-looking string as a literal id.
    def test_exact_json_object_string_is_literal(self):
        assert filter_matches('{"a":"b"}', include_filter='{"a":"b"}', exact=True)
        assert not filter_matches("other", include_filter='{"a":"b"}', exact=True)

    def test_exact_json_array_string_is_literal(self):
        assert filter_matches('["x","y"]', include_filter='["x","y"]', exact=True)
        # must NOT be parsed as a 2-element list of ids
        assert not filter_matches("x", include_filter='["x","y"]', exact=True)

    # #4 — non-str candidates are coerced, not crashed.
    def test_numeric_candidate_coerced(self):
        assert filter_matches(123, include_filter="123") is True  # type: ignore[arg-type]
        assert (
            FilterPattern.from_filters(include_filter=["123"], exact=True).matches(123)  # type: ignore[arg-type]
            is True
        )
        assert filter_matches(456, include_filter="123") is False  # type: ignore[arg-type]


def test_mixed_dict_one_malformed_key_raises():
    """A valid key + one bare-string schema value must raise (per-key check),
    not silently drop the malformed key (under-match)."""
    import pytest

    with pytest.raises(ValueError, match=r"key\(s\).*\^db2\$"):
        FilterPattern.from_filters(
            include_filter={"^db1$": ["^s$"], "^db2$": "^scalar$"}
        )


def test_valid_multi_key_dict_matches_each():
    p = FilterPattern.from_filters(
        include_filter={"^db1$": ["^s1$"], "^db2$": ["^s2$"]}
    )
    assert p.matches("db1.s1") is True
    assert p.matches("db2.s2") is True
    assert p.matches("db1.s2") is False  # cross-product not allowed
    assert p.matches("db3.s1") is False
