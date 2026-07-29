"""JSON-envelope handling in the filter deny-list (CONNECT-551).

``validate_filter_no_sql_injection`` parses ``{...}``-shaped strings before
applying the deny-list, precisely so a legacy filter-as-JSON-string isn't
rejected for the double-quotes JSON syntax requires. That escape hatch only
covers *parseable* JSON: when ``orjson.loads`` fails, the raw JSON text falls
through to the deny-list and is reported as a SQL-injection attempt on a
character the caller never authored.

The invariant these tests pin is about the *verdict*, not about acceptance —
a malformed filter is still rejected, just for the right reason:

* A ``{...}`` envelope whose contents are deny-list-clean must never be
  reported as SQL-unsafe. It is rejected as a malformed filter naming the
  parse position instead.
* An envelope carrying a genuine injection must still get the security
  verdict, parseable or not — a stray backslash is not a way to hide a
  forbidden sequence from the deny-list.
* The dict, bare-regex and valid-JSON-string paths must be untouched, as must
  a brace-wrapped raw regex, which was never a JSON attempt at all.
"""

from __future__ import annotations

import pytest

from application_sdk.common.sql_filters import (
    prepare_filters,
    validate_filter_no_sql_injection,
)
from application_sdk.common.sql_filters_errors import InvalidSqlFilterError

# The exact ``include_filter`` from the production run in CONNECT-551. ``\_``
# is a SQL-LIKE escape the producer serialised into a JSON string without
# escaping the backslash, so it is not legal JSON.
PRODUCTION_INCLUDE_FILTER = r'{"^d\_edw\_stg\_03$": []}'

ACCEPTED = "accepted"
UNSAFE = "rejected_as_unsafe"
MALFORMED = "rejected_as_malformed"


def _verdict(value: object) -> str:
    """Classify the validator's outcome for *value*.

    ``UNSAFE`` is reserved for the deny-list's SQL-injection verdict; any other
    rejection (a typed malformed-filter error, a parse error) is ``MALFORMED``.
    """
    try:
        validate_filter_no_sql_injection(value)
    except Exception as exc:  # noqa: BLE001 - classifying, not handling
        return UNSAFE if "SQL-unsafe sequence" in str(exc) else MALFORMED
    return ACCEPTED


def _prepare_filters_verdict(include: str) -> str:
    """Same classification, through the production include-filter path."""
    try:
        prepare_filters(include, "{}")
    except Exception as exc:  # noqa: BLE001 - classifying, not handling
        return UNSAFE if "SQL-unsafe sequence" in str(exc) else MALFORMED
    return ACCEPTED


# ---------------------------------------------------------------------------
# The regression: a clean-content envelope must never be called SQL-unsafe.
# ---------------------------------------------------------------------------


class TestUnparseableEnvelopeIsNotASecurityVerdict:
    @pytest.mark.parametrize(
        ("label", "value"),
        [
            # The production failure. `\_` is not a legal JSON escape.
            ("sql_like_escape", PRODUCTION_INCLUDE_FILTER),
            # Same defect, reached without a `_`: any stray backslash does it.
            ("windows_path_key", r'{"C:\temp\staging$": []}'),
            # A `\u` that isn't followed by four hex digits.
            ("bad_unicode_escape", r'{"^stg\uZZZZ$": []}'),
            # Structurally broken but content-clean — still not an injection.
            ("unterminated_list", r'{"^stg\_a$": [}'),
            ("trailing_comma", '{"^stg_a$": [],}'),
            # The braces guard strips whitespace, so padding must not change
            # the verdict either.
            ("whitespace_padded", f"  {PRODUCTION_INCLUDE_FILTER}  "),
        ],
    )
    def test_validator_does_not_report_sql_unsafe(self, label: str, value: str) -> None:
        assert _verdict(value) != UNSAFE, (
            f"{label}: a malformed filter must not be reported as a SQL-injection "
            "attempt on the JSON envelope's own quote"
        )

    @pytest.mark.parametrize(
        "value",
        [
            PRODUCTION_INCLUDE_FILTER,
            r'{"C:\temp\staging$": []}',
            r'{"^stg\uZZZZ$": []}',
        ],
    )
    def test_prepare_filters_does_not_report_sql_unsafe(self, value: str) -> None:
        # The validator is only half the path: ``prepare_filters`` also runs
        # ``parse_filter_input``, which JSON-decodes independently. Tolerating
        # the value in the validator alone still fails a step later, so the
        # invariant has to hold end-to-end.
        assert _prepare_filters_verdict(value) != UNSAFE

    def test_rejection_names_the_parse_problem(self) -> None:
        # The message has to be actionable: point at the malformed JSON, not at
        # a forbidden character. The cause carries the parse position.
        with pytest.raises(InvalidSqlFilterError) as exc_info:
            validate_filter_no_sql_injection(PRODUCTION_INCLUDE_FILTER)
        assert "json" in str(exc_info.value).lower()
        assert "column 6" in str(exc_info.value.__cause__)


# ---------------------------------------------------------------------------
# Genuine injections must still be rejected — parseable envelope or not.
# ---------------------------------------------------------------------------


class TestInjectionStillRejected:
    @pytest.mark.parametrize(
        ("label", "value"),
        [
            # Valid JSON, injection in the parsed value. Already covered by
            # the existing suite; re-pinned here because the fix touches the
            # branch that decides whether the parse happens at all.
            ("valid_json_quote_in_value", '{"^prod$": ["sch\'; DROP TABLE x"]}'),
            ("valid_json_comment_in_key", '{"^prod--$": []}'),
            # Unparseable *and* injecting. Whatever the fallback is, it must
            # not be "tolerate".
            ("broken_json_stacked_query", '{"a"; DROP TABLE x": []}'),
            ("backslash_plus_trailing_stmt", r'{"^a\_b$": []} ; DROP TABLE x --}'),
            ("backslash_plus_line_comment", r'{"^a\_b--c$": []}'),
            ("backslash_plus_single_quote", r"{\"^a\_b$\": ['x'] }"),
            ("brace_wrapped_not_json_at_all", "{ not really json ' }"),
            # A lenient re-escape must not be a way to smuggle a null byte or
            # a block comment past the deny-list.
            ("backslash_plus_null_byte", '{"^a\\_b\x00$": []}'),
            ("backslash_plus_block_comment", r'{"^a\_b/*c$": []}'),
        ],
    )
    def test_rejected(self, label: str, value: str) -> None:
        assert _verdict(value) != ACCEPTED, f"{label} must not be accepted"

    @pytest.mark.parametrize(
        "value",
        [
            r'{"^a\_b--c$": []}',
            r'{"^a\_b/*c$": []}',
            '{"^a\\_b\x00$": []}',
        ],
    )
    def test_deny_list_still_reached_through_a_backslash_envelope(
        self, value: str
    ) -> None:
        # Stronger than "rejected": a stray backslash must not become a way to
        # hide a forbidden sequence from the deny-list. The verdict for these
        # has to be the security one, since the *content* is genuinely unsafe.
        assert _verdict(value) == UNSAFE


# ---------------------------------------------------------------------------
# Everything that works today keeps working.
# ---------------------------------------------------------------------------


class TestUnchangedPaths:
    @pytest.mark.parametrize(
        ("label", "value"),
        [
            ("dict_plain_underscore", {"^DEFAULT$": ["^D_EDW_STG_03$"]}),
            ("dict_escaped_underscore", {"^d\\_edw\\_stg\\_03$": []}),
            ("dict_empty", {}),
            ("dict_object_shaped_value", {"^db$": {"^sch$": {}}}),
        ],
    )
    def test_dict_paths_pass(self, label: str, value: dict) -> None:
        assert validate_filter_no_sql_injection(value) == value

    @pytest.mark.parametrize(
        ("label", "value"),
        [
            ("json_string_plain_underscore", '{"^d_edw_stg_03$": []}'),
            # Correctly escaped backslash — what the producer should emit.
            ("json_string_escaped_backslash", '{"^d\\\\_edw$": []}'),
            ("json_string_empty_object", "{}"),
            ("bare_regex", "d_edw_stg_03"),
            ("bare_regex_with_backslash", r"^d\_edw\_stg\_03$"),
            ("bare_regex_anchored_alternation", r"^(prod|stage)_db\.[a-z]+$"),
            # Brace-wrapped but quote-free: never a JSON attempt, so it keeps
            # raw-regex handling instead of becoming a malformed-filter error.
            # ``_prepare_sql``'s no-cascade tests rely on exactly this shape.
            ("brace_wrapped_raw_regex", "{normalized_include_regex}"),
            ("brace_wrapped_quantifier", "{2,3}"),
        ],
    )
    def test_string_paths_pass_unchanged(self, label: str, value: str) -> None:
        assert validate_filter_no_sql_injection(value) == value

    def test_correctly_escaped_json_string_still_normalises(self) -> None:
        include, exclude = prepare_filters('{"^d\\\\_edw$": []}', "{}")
        assert include == "^d\\_edw\\..*$"
        assert exclude == "^$"


# ---------------------------------------------------------------------------
# The production entry point: input decoding on the typed contract.
# ---------------------------------------------------------------------------


class TestExtractionInputDecoding:
    """``ExtractionInput``'s after-validator is where the incident surfaced.

    Also pins the coupling to the worker fix: because the malformed-filter error
    is an ``AppError`` rather than a ``ValueError``, Pydantic propagates it raw
    instead of collecting it into a ``ValidationError``. That is only safe to
    fail a run on because ``AppError`` is declared in
    ``_WORKFLOW_FAILURE_EXCEPTION_TYPES`` — see the worker tests.
    """

    def test_malformed_json_filter_raises_the_typed_error(self) -> None:
        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        with pytest.raises(InvalidSqlFilterError) as exc_info:
            ExtractionInput(include_filter=PRODUCTION_INCLUDE_FILTER)
        assert exc_info.value.code == "INVALID_INPUT_SQL_FILTER_JSON"
        assert "SQL-unsafe sequence" not in str(exc_info.value)

    def test_injection_in_filter_still_fails_validation(self) -> None:
        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        with pytest.raises(Exception, match=r"SQL-unsafe sequence") as exc_info:
            ExtractionInput(include_filter='{"^prod$": ["sch\'; DROP TABLE x"]}')
        assert exc_info.value is not None

    def test_valid_filter_decodes(self) -> None:
        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        decoded = ExtractionInput(include_filter='{"^d_edw_stg_03$": []}')
        assert decoded.include_filter
