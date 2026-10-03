"""``get_database_names`` — when the include-filter may stand in for discovery.

The include-filter's keys are regular expressions. ``get_database_names`` has a
shortcut that returns them as database names directly, skipping the SQL round
trip. That shortcut is only sound for keys whose sole match is themselves; for a
genuine pattern the source has to be asked which databases exist.

Regression coverage for the phantom-catalog bug: ``^benchmark_.*$`` was being
reduced to the literal ``benchmark_`` by a non-word-character strip, and because
that produced a non-empty list, SQL discovery was skipped entirely. The crawl
then ran against one database that does not exist and silently lost every
database the pattern was meant to select. Found via atlan-trino-app#118.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.common.sql_filters import get_database_names

#: Shaped like a real connector's discovery query — both filter placeholders
#: present. A placeholder-free string would let ``prepare_query`` no-op, so the
#: tests that assert on the SQL actually sent would pass vacuously.
#: Mirrors ``atlan-trino-app``'s ``app/sql/extract_database.sql``.
_FETCH_SQL = """SELECT catalogs.table_cat AS database_name
FROM system.jdbc.catalogs AS catalogs
WHERE regexp_like(catalogs.table_cat, {include_databases})
    AND NOT regexp_like(catalogs.table_cat, {exclude_databases})"""


def _sql_client(*database_names: str) -> MagicMock:
    """A client whose discovery query reports ``database_names``."""
    client = MagicMock()
    client.get_results = AsyncMock(return_value={"database_name": list(database_names)})
    return client


def _args(include_filter: object) -> dict:
    return {"metadata": {"include-filter": include_filter}}


class TestAnchoredLiteralKeysSkipDiscovery:
    """The shortcut itself — a fully anchored literal is its own only match."""

    async def test_anchored_literal_is_used_without_querying(self) -> None:
        client = _sql_client("should_not_be_consulted")
        names = await get_database_names(
            client, _args({"^mydb$": ["^public$"]}), _FETCH_SQL
        )
        assert names == ["mydb"]
        client.get_results.assert_not_called()

    async def test_several_anchored_literals_are_all_used(self) -> None:
        client = _sql_client("should_not_be_consulted")
        names = await get_database_names(
            client, _args({"^alpha$": ["*"], "^beta$": ["*"]}), _FETCH_SQL
        )
        assert sorted(names) == ["alpha", "beta"]
        client.get_results.assert_not_called()


class TestPatternKeysFallThroughToDiscovery:
    """A key matching a *set* of databases must be resolved against the source."""

    async def test_regex_key_queries_the_source(self) -> None:
        client = _sql_client("benchmark_1", "benchmark_2")
        names = await get_database_names(
            client, _args({"^benchmark_.*$": ["^tiny$"]}), _FETCH_SQL
        )
        assert names == ["benchmark_1", "benchmark_2"]
        client.get_results.assert_awaited_once()

    async def test_regex_key_never_yields_the_de_metacharacterd_pattern(self) -> None:
        """The specific defect: ``^benchmark_.*$`` must not become ``benchmark_``.

        ``benchmark_`` is not a catalog on the source, so emitting it produces a
        phantom database asset and drops everything beneath it.
        """
        client = _sql_client("benchmark_1", "benchmark_2")
        names = await get_database_names(
            client, _args({"^benchmark_.*$": ["^tiny$"]}), _FETCH_SQL
        )
        assert "benchmark_" not in names

    async def test_interior_metacharacters_query_the_source(self) -> None:
        client = _sql_client("bench_one_mark")
        names = await get_database_names(
            client, _args({"^bench.*mark$": ["*"]}), _FETCH_SQL
        )
        assert names == ["bench_one_mark"]
        client.get_results.assert_awaited_once()

    async def test_alternation_queries_the_source(self) -> None:
        client = _sql_client("alpha", "beta")
        names = await get_database_names(
            client, _args({"^(alpha|beta)$": ["*"]}), _FETCH_SQL
        )
        assert names == ["alpha", "beta"]
        client.get_results.assert_awaited_once()

    @pytest.mark.parametrize(
        "key",
        [
            "^benchmark_",  # starts-with — the original bug, reached without metachars
            "benchmark_$",  # ends-with
            "benchmark_",  # contains
        ],
    )
    async def test_a_partly_anchored_key_queries_the_source(self, key: str) -> None:
        """Anchors are load-bearing: without both, the key matches a *set*.

        This is the case a metacharacter-only check misses. ``^benchmark_``
        contains no metacharacters, so it looks literal — but it means "starts
        with benchmark_", and treating it as the name ``benchmark_`` reproduces
        the exact phantom database the fix exists to prevent.

        Asserts only that the source was consulted and that the phantom name is
        never invented. What each key then *selects* differs by key — ``^x``
        starts-with vs ``x$`` ends-with vs ``x`` contains — and that belongs in
        ``TestDiscoveredNamesAreFilteredInPython``, which gives each shape
        fixture data appropriate to its semantics.
        """
        client = _sql_client("benchmark_1", "benchmark_2")
        names = await get_database_names(client, _args({key: ["*"]}), _FETCH_SQL)
        assert "benchmark_" not in names
        client.get_results.assert_awaited_once()

    async def test_one_pattern_among_literals_still_queries_the_source(self) -> None:
        """Mixed keys go to SQL wholesale.

        The prepared query is built from the whole include-filter, so it resolves
        the literal keys too — returning a partial list built from the literals
        plus a SQL lookup for the rest would be strictly worse.
        """
        client = _sql_client("alpha", "benchmark_1")
        names = await get_database_names(
            client, _args({"^alpha$": ["*"], "^benchmark_.*$": ["*"]}), _FETCH_SQL
        )
        assert names == ["alpha", "benchmark_1"]
        client.get_results.assert_awaited_once()


class TestDiscoveredNamesAreFilteredInPython:
    """The half that SQL cannot do.

    ``extract_database_names_from_regex_common`` validates filter keys against an
    identifier pattern and drops anything else, degrading the predicate to
    ``'.*'``. So the discovery query returns *everything* and the pattern has to
    be applied to its results here.

    Every test in this class supplies at least one database the filter must
    **reject**. Without that, a filter that is silently ignored still looks
    correct — which is exactly how this defect survived its first fix attempt
    and would have false-passed atlan-trino-app#118.
    """

    async def test_regex_key_excludes_non_matching_databases(self) -> None:
        client = _sql_client("benchmark_1", "benchmark_2", "production", "staging")
        names = await get_database_names(
            client, _args({"^benchmark_.*$": ["^tiny$"]}), _FETCH_SQL
        )
        assert names == ["benchmark_1", "benchmark_2"]
        assert "production" not in names and "staging" not in names

    async def test_starts_with_key_keeps_prefix_semantics(self) -> None:
        """``^bench`` means starts-with, so it must keep ``bench_1``, not demand equality."""
        client = _sql_client("bench_1", "bench_2", "notbench", "production")
        names = await get_database_names(client, _args({"^bench": ["*"]}), _FETCH_SQL)
        assert names == ["bench_1", "bench_2"]

    async def test_unanchored_key_keeps_contains_semantics(self) -> None:
        client = _sql_client("my_bench_db", "bench", "production")
        names = await get_database_names(client, _args({"bench": ["*"]}), _FETCH_SQL)
        assert names == ["my_bench_db", "bench"]

    async def test_alternation_key_selects_both_and_rejects_others(self) -> None:
        client = _sql_client("alpha", "beta", "gamma")
        names = await get_database_names(
            client, _args({"^(alpha|beta)$": ["*"]}), _FETCH_SQL
        )
        assert names == ["alpha", "beta"]

    async def test_several_pattern_keys_union(self) -> None:
        client = _sql_client("alpha_1", "beta_1", "gamma_1")
        names = await get_database_names(
            client, _args({"^alpha.*$": ["*"], "^beta.*$": ["*"]}), _FETCH_SQL
        )
        assert names == ["alpha_1", "beta_1"]

    async def test_pattern_matching_nothing_returns_empty_not_everything(self) -> None:
        """A filter that matches no database must yield none — not fall open."""
        client = _sql_client("production", "staging")
        names = await get_database_names(
            client, _args({"^benchmark_.*$": ["*"]}), _FETCH_SQL
        )
        assert names == []

    async def test_unparseable_key_degrades_to_literal_comparison(self) -> None:
        """An invalid regex compares literally rather than dropping the database."""
        client = _sql_client("db[1", "other")
        names = await get_database_names(client, _args({"^db[1$": ["*"]}), _FETCH_SQL)
        assert names == ["db[1"]


class TestDiscoveryQueryIsNotNarrowedByTheBrokenPredicate:
    """Assertions on the SQL *sent*, not just on the mocked rows returned.

    Every other test here mocks ``get_results``, so it cannot see what the query
    asked for — and that blind spot hid a real defect. The SQL builder keeps only
    include-filter keys that look like identifiers and drops the rest, so a
    filter mixing shapes narrowed the predicate to the literal alone and the
    pattern's databases were never returned. Python filtering cannot recover
    rows the query never produced.
    """

    async def test_include_filter_is_withheld_from_the_discovery_query(self) -> None:
        client = _sql_client("prod", "bench_1")
        await get_database_names(
            client, _args({"^prod$": [], "^bench.*$": []}), _FETCH_SQL
        )
        sent = client.get_results.await_args.args[0]
        assert "'^(prod)$'" not in sent, (
            "discovery query was narrowed to the literal key only, so databases "
            f"matching '^bench.*$' can never be returned:\n{sent}"
        )
        assert "'.*'" in sent

    async def test_mixed_literal_and_pattern_keys_return_both(self) -> None:
        """The end-to-end consequence of the above."""
        client = _sql_client("prod", "bench_1", "bench_2", "unrelated")
        names = await get_database_names(
            client, _args({"^prod$": [], "^bench.*$": []}), _FETCH_SQL
        )
        assert names == ["prod", "bench_1", "bench_2"]
        assert "unrelated" not in names

    async def test_exclude_filter_still_reaches_the_query(self) -> None:
        """Withholding the include filter must not take the exclude with it."""
        client = _sql_client("bench_1")
        args = _args({"^bench.*$": []})
        args["metadata"]["exclude-filter"] = '{"^secret$": []}'
        await get_database_names(client, args, _FETCH_SQL)
        sent = client.get_results.await_args.args[0]
        assert "'^(secret)$'" in sent, f"exclude predicate was lost:\n{sent}"

    async def test_callers_workflow_args_are_not_mutated(self) -> None:
        """These args are shared with the rest of the workflow."""
        client = _sql_client("bench_1")
        args = _args({"^bench.*$": []})
        await get_database_names(client, args, _FETCH_SQL)
        assert args["metadata"]["include-filter"] == {"^bench.*$": []}


class TestNoFilterFallsThroughToDiscovery:
    """Pre-existing behaviour, pinned so the fix cannot change it."""

    @pytest.mark.parametrize("empty", [{}, "", None])
    async def test_absent_filter_queries_the_source(self, empty: object) -> None:
        client = _sql_client("alpha", "beta")
        names = await get_database_names(client, _args(empty), _FETCH_SQL)
        assert names == ["alpha", "beta"]
        client.get_results.assert_awaited_once()
