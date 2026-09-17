"""``expected_asset_attributes`` on ``BaseE2ETest``, end to end (FND-2094).

Three claims, in the order a run makes them:

1. declaring the knob makes the harness *read* attribute values — the types it
   names join the count probe, and the reader is called with the attributes it
   asked for;
2. the values are *graded*, and a wrong one fails the leg with a message naming
   the asset, the attribute, what was seen and what was expected;
3. a search that could not be read is *ungraded* — the same
   ``AtlasReadIndeterminateError`` the counts and depths already raise, never an
   assertion about the connector.

The control running through all three is the distinction the knob exists for: a
present zero and an absent attribute are the same number to a count assertion
and must never be the same answer here.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.testing.e2e._errors import AtlasReadIndeterminateError
from application_sdk.testing.e2e.base import BaseE2ETest, DAGSpec, FullDAGOutcome
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.harness.expectations import (
    AssetAttributes,
    AssetRef,
    AtLeast,
    Exactly,
    Present,
    Unreadable,
)

_QN = "default/trino/1700000000123456"


class _Trino(BaseE2ETest):
    """The motivating shape: a hermetic fixture whose counts are knowable.

    8 tables, 0 views, 1 schema — so every value below is an exact pin, and
    ``viewsCount: 0`` is the one a count assertion structurally cannot make.
    """

    connector_short_name = "trino"
    argo_package_name = "@atlan/trino"
    argo_template_name = "atlan-trino"
    expect_lineage = False
    required_dag_nodes = ("extract",)
    expected_min_asset_counts = {"Database": 1, "Schema": 1}
    expected_asset_attributes = {
        "Schema": {"tableCount": 8, "viewsCount": 0},
        "Database": {"schemaCount": 1},
    }


class _NoAttributeCheck(BaseE2ETest):
    connector_short_name = "trino"
    argo_package_name = "@atlan/trino"
    argo_template_name = "atlan-trino"
    expect_lineage = False
    required_dag_nodes = ("extract",)
    expected_min_asset_counts = {"Schema": 1}


def _suite(cls: type[BaseE2ETest] = _Trino) -> BaseE2ETest:
    suite = cls()
    # setup_method (which mints this) isn't run in pure unit tests.
    suite.connection_qualified_name = _QN
    return suite


def _sample(qualified_name: str, **values: object) -> AssetAttributes:
    return AssetAttributes(qualified_name=qualified_name, values=values)


def _succeeded() -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.SUCCEEDED,
        nodes=[
            DAGNodeResult(
                name="extract",
                status=DAGNodeStatus.SUCCEEDED,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
        ],
    )


def _outcome(attribute_reads: Any) -> FullDAGOutcome:
    """A run that passed every other assertion, so only the values are on trial."""
    return FullDAGOutcome(
        ae_result=_succeeded(),
        connection_qualified_name=_QN,
        connection_in_atlas=True,
        asset_counts={"Database": 1, "Schema": 1},
        asset_count_reads={"Database": 1, "Schema": 1},
        total_asset_read=2,
        asset_attribute_reads=attribute_reads,
    )


def _correct_reads() -> dict[str, list[AssetAttributes]]:
    return {
        "Schema": [_sample(f"{_QN}/db/sch", tableCount=8, viewsCount=0)],
        "Database": [_sample(f"{_QN}/db", schemaCount=1)],
    }


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


class TestDeclaration:
    def test_the_declared_types_join_the_count_probe(self) -> None:
        # Otherwise the sample would be read before ES had indexed the type.
        dag = _suite()._dag
        assert set(dag.expected_asset_attributes) == {"Schema", "Database"}

    def test_a_bare_scalar_is_coerced_to_an_exact_matcher(self) -> None:
        attributes = _suite()._asset_expectations().attributes
        assert attributes["Schema"]["tableCount"] == Exactly(8)
        assert attributes["Schema"]["viewsCount"] == Exactly(0)

    def test_a_dag_spec_overrides_the_class_declaration(self) -> None:
        suite = _suite()
        resolved = suite.resolve_dag(
            DAGSpec(expected_asset_attributes={"Schema": {"tableCount": AtLeast(1)}})
        )
        assert resolved.expected_asset_attributes == {
            "Schema": {"tableCount": AtLeast(1)}
        }

    def test_a_dag_spec_that_says_nothing_inherits_the_class(self) -> None:
        suite = _suite()
        assert (
            suite.resolve_dag(DAGSpec()).expected_asset_attributes
            == _Trino.expected_asset_attributes
        )

    def test_a_suite_that_declares_nothing_has_no_attribute_expectations(self) -> None:
        assert _suite(_NoAttributeCheck)._asset_expectations().attributes == {}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


class TestGrading:
    def test_correct_values_pass(self) -> None:
        _suite()._assert_full_dag_outcome(_outcome(_correct_reads()))  # must not raise

    def test_a_degraded_zero_fails_and_names_the_asset_and_the_attribute(self) -> None:
        # The motivating defect: a swallowed permissions error substitutes 0 and
        # publishes it as fact. Structurally the tree is perfect.
        reads = _correct_reads()
        reads["Database"] = [_sample(f"{_QN}/db", schemaCount=0)]
        with pytest.raises(AssertionError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(reads))
        message = str(exc.value)
        assert "Database.schemaCount" in message
        assert f"{_QN}/db" in message
        assert "= 0" in message
        assert "expected exactly 1" in message

    def test_an_attribute_that_stopped_being_set_fails_where_a_count_cannot(
        self,
    ) -> None:
        # viewsCount == 0 and viewsCount absent are the same number to every
        # count assertion in the harness, and different states in Atlas.
        reads = _correct_reads()
        reads["Schema"] = [_sample(f"{_QN}/db/sch", tableCount=8)]
        with pytest.raises(AssertionError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(reads))
        assert "Schema.viewsCount" in str(exc.value)
        assert "is absent (attribute not set)" in str(exc.value)

    def test_the_failure_says_the_counts_were_fine(self) -> None:
        # So a reader does not go hunting the extract path for a values bug.
        reads = _correct_reads()
        reads["Schema"] = [_sample(f"{_QN}/db/sch", tableCount=3, viewsCount=0)]
        with pytest.raises(AssertionError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(reads))
        assert "the right number of assets landed in the right place" in str(exc.value)

    def test_every_unmet_matcher_is_reported_not_just_the_first(self) -> None:
        reads = {
            "Schema": [_sample(f"{_QN}/db/sch", tableCount=3)],
            "Database": [_sample(f"{_QN}/db", schemaCount=0)],
        }
        failures = _suite()._validate_asset_attributes(reads)
        assert len(failures) == 3  # tableCount, viewsCount (absent), schemaCount

    def test_a_suite_that_declares_nothing_grades_nothing(self) -> None:
        suite = _suite(_NoAttributeCheck)
        outcome = FullDAGOutcome(
            ae_result=_succeeded(),
            connection_qualified_name=_QN,
            connection_in_atlas=True,
            asset_counts={"Schema": 1},
            asset_count_reads={"Schema": 1},
            total_asset_read=1,
            asset_attribute_reads={"Schema": [_sample(f"{_QN}/db/sch", tableCount=0)]},
        )
        suite._assert_full_dag_outcome(outcome)  # must not raise


# ---------------------------------------------------------------------------
# Unreadable
# ---------------------------------------------------------------------------


class TestUnreadable:
    def test_an_unreadable_attribute_read_is_ungraded_not_a_failure(self) -> None:
        reads = _correct_reads()
        reads["Schema"] = Unreadable(cause=RuntimeError("atlas is down"))
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(reads))
        assert not isinstance(exc.value, AssertionError)
        assert exc.value.checks is not None and "Schema" in exc.value.checks

    def test_an_empty_sample_is_skipped(self) -> None:
        # The control: "the type landed nothing" is the count floors' job, which
        # is exactly why a failed read may not be spelled as an empty list.
        reads = _correct_reads()
        reads["Schema"] = []
        _suite()._assert_full_dag_outcome(_outcome(reads))  # must not raise


# ---------------------------------------------------------------------------
# The read itself
# ---------------------------------------------------------------------------


class TestTheRead:
    async def test_the_reader_is_called_with_the_declared_attributes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.testing.e2e import base as base_module

        calls: list[dict[str, Any]] = []

        async def _fake_sample_attributes(
            _client: Any,
            connection_qualified_name: str,
            type_attributes: Any,
            *,
            per_type: int,
        ) -> Any:
            calls.append(
                {
                    "connection": connection_qualified_name,
                    "type_attributes": {
                        name: tuple(attrs) for name, attrs in type_attributes.items()
                    },
                    "per_type": per_type,
                }
            )
            from datetime import timedelta

            from application_sdk.testing.harness.outcome import Settled

            return Settled(
                label="attrs",
                attempts=1,
                elapsed=timedelta(0),
                value=_correct_reads(),
            )

        suite = _suite()
        monkeypatch.setattr(
            base_module.atlas, "sample_asset_attributes", _fake_sample_attributes
        )
        monkeypatch.setattr(
            base_module.atlas,
            "count_total_assets",
            _settled(2),
        )
        monkeypatch.setattr(
            base_module.atlas,
            "count_assets",
            _settled({"Database": 1, "Schema": 1}),
        )

        outcome = await suite._read_inventory(object(), _succeeded())

        assert calls == [
            {
                "connection": _QN,
                "type_attributes": {
                    "Schema": ("tableCount", "viewsCount"),
                    "Database": ("schemaCount",),
                },
                "per_type": 3,
            }
        ]
        assert outcome.asset_attribute_samples == _correct_reads()

    async def test_a_suite_that_declares_nothing_makes_no_attribute_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.testing.e2e import base as base_module

        called = False

        async def _never(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal called
            called = True
            raise AssertionError("the attribute reader must not be called")

        monkeypatch.setattr(base_module.atlas, "sample_asset_attributes", _never)
        monkeypatch.setattr(base_module.atlas, "count_total_assets", _settled(1))
        monkeypatch.setattr(base_module.atlas, "count_assets", _settled({"Schema": 1}))

        await _suite(_NoAttributeCheck)._read_inventory(object(), _succeeded())
        assert not called


def _settled(value: Any) -> Any:
    """A reader stub answering ``Settled(value)`` whatever it is asked.

    Args:
        value: What the read should report.

    Returns:
        An async callable with the readers' shape.
    """
    from datetime import timedelta

    from application_sdk.testing.harness.outcome import Settled

    async def _read(*_args: Any, **_kwargs: Any) -> Any:
        return Settled(label="stub", attempts=1, elapsed=timedelta(0), value=value)

    return _read


# ---------------------------------------------------------------------------
# Per-asset addressing: values that differ WITHIN a type
# ---------------------------------------------------------------------------


class _FiveSchemas(BaseE2ETest):
    """The case the per-type knob cannot express.

    Two schemas of one type at different view counts: no per-type claim about
    ``viewsCount`` survives beyond ``Present()``, so the numbers themselves are
    pinned per asset. Both knobs are declared here because that is how they are
    meant to be used together — the type-wide claim that holds, plus the values.
    """

    connector_short_name = "trino"
    argo_package_name = "@atlan/trino"
    argo_template_name = "atlan-trino"
    expect_lineage = False
    required_dag_nodes = ("extract",)
    expected_min_asset_counts = {"Schema": 2}
    expected_asset_attributes = {"Schema": {"viewsCount": Present()}}
    expected_asset_attributes_at = {
        "Schema": {
            "sch_empty": {"viewsCount": 0, "tableCount": 3},
            "sch_busy": {"viewsCount": 10, "tableCount": 8},
        }
    }


def _ref(suffix: str, type_name: str = "Schema") -> AssetRef:
    return AssetRef(type_name=type_name, qualified_name_suffix=suffix)


def _addressed_right() -> dict[AssetRef, list[AssetAttributes]]:
    return {
        _ref("sch_empty"): [_sample(f"{_QN}/db/sch_empty", viewsCount=0, tableCount=3)],
        _ref("sch_busy"): [_sample(f"{_QN}/db/sch_busy", viewsCount=10, tableCount=8)],
    }


def _five_schema_outcome(addressed: Any) -> FullDAGOutcome:
    """A run whose counts and per-type matchers all pass, so only values differ."""
    return FullDAGOutcome(
        ae_result=_succeeded(),
        connection_qualified_name=_QN,
        connection_in_atlas=True,
        asset_counts={"Schema": 2},
        asset_count_reads={"Schema": 2},
        total_asset_read=2,
        asset_attribute_reads={
            "Schema": [
                _sample(f"{_QN}/db/sch_empty", viewsCount=0),
                _sample(f"{_QN}/db/sch_busy", viewsCount=10),
            ]
        },
        addressed_attribute_reads=addressed,
    )


class TestPerAssetAddressing:
    def test_two_schemas_at_different_values_both_pass(self) -> None:
        suite = _suite(_FiveSchemas)
        suite._assert_full_dag_outcome(_five_schema_outcome(_addressed_right()))

    def test_a_value_wrong_on_one_schema_fails_and_names_that_schema(self) -> None:
        addressed = _addressed_right()
        addressed[_ref("sch_busy")] = [
            _sample(f"{_QN}/db/sch_busy", viewsCount=0, tableCount=8)
        ]
        with pytest.raises(AssertionError) as exc:
            _suite(_FiveSchemas)._assert_full_dag_outcome(
                _five_schema_outcome(addressed)
            )
        message = str(exc.value)
        assert "Schema[sch_busy].viewsCount" in message
        assert f"{_QN}/db/sch_busy" in message
        assert "expected exactly 10" in message
        # The other schema's genuine 0 is not dragged in.
        assert "sch_empty" not in message

    def test_the_per_type_check_cannot_see_what_this_catches(self) -> None:
        # The control that justifies the knob. Swap the two schemas' values and
        # every per-type matcher still passes — both are still Present() — while
        # the addressed check reports both.
        swapped = {
            "Schema": [
                _sample(f"{_QN}/db/sch_empty", viewsCount=10),
                _sample(f"{_QN}/db/sch_busy", viewsCount=0),
            ]
        }
        suite = _suite(_FiveSchemas)
        assert suite._validate_asset_attributes(swapped) == []
        addressed = {
            _ref("sch_empty"): [
                _sample(f"{_QN}/db/sch_empty", viewsCount=10, tableCount=3)
            ],
            _ref("sch_busy"): [
                _sample(f"{_QN}/db/sch_busy", viewsCount=0, tableCount=8)
            ],
        }
        assert len(suite._validate_addressed_asset_attributes(addressed)) == 2

    def test_an_addressed_asset_that_did_not_land_fails(self) -> None:
        addressed = _addressed_right()
        addressed[_ref("sch_empty")] = []
        with pytest.raises(AssertionError) as exc:
            _suite(_FiveSchemas)._assert_full_dag_outcome(
                _five_schema_outcome(addressed)
            )
        assert "Schema[sch_empty]" in str(exc.value)
        assert "did not land" in str(exc.value)

    def test_an_ambiguous_suffix_fails(self) -> None:
        addressed = _addressed_right()
        addressed[_ref("sch_busy")] = [
            _sample(f"{_QN}/db1/sch_busy", viewsCount=10, tableCount=8),
            _sample(f"{_QN}/db2/sch_busy", viewsCount=10, tableCount=8),
        ]
        with pytest.raises(AssertionError, match="lengthen the suffix"):
            _suite(_FiveSchemas)._assert_full_dag_outcome(
                _five_schema_outcome(addressed)
            )

    def test_an_unreadable_addressed_read_is_ungraded(self) -> None:
        addressed: dict[AssetRef, Any] = dict(_addressed_right())
        addressed[_ref("sch_busy")] = Unreadable(cause=RuntimeError("atlas down"))
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            _suite(_FiveSchemas)._assert_full_dag_outcome(
                _five_schema_outcome(addressed)
            )
        assert not isinstance(exc.value, AssertionError)

    def test_the_declared_types_join_the_count_probe(self) -> None:
        # An addressed Schema needs the Schema count poll to have waited for ES.
        assert "Schema" in _suite(_FiveSchemas)._dag.expected_asset_attributes_at

    def test_the_nested_declaration_flattens_to_refs(self) -> None:
        attributes_at = _suite(_FiveSchemas)._asset_expectations().attributes_at
        assert set(attributes_at) == {_ref("sch_empty"), _ref("sch_busy")}
        assert attributes_at[_ref("sch_empty")]["viewsCount"] == Exactly(0)

    def test_a_dag_spec_overrides_the_class_declaration(self) -> None:
        resolved = _suite(_FiveSchemas).resolve_dag(
            DAGSpec(expected_asset_attributes_at={"Table": {"orders": {"rowCount": 5}}})
        )
        assert resolved.expected_asset_attributes_at == {
            "Table": {"orders": {"rowCount": 5}}
        }

    def test_a_suite_that_declares_nothing_addresses_nothing(self) -> None:
        assert _suite(_NoAttributeCheck)._asset_expectations().attributes_at == {}

    async def test_the_reader_is_called_with_the_flattened_refs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import timedelta

        from application_sdk.testing.e2e import base as base_module
        from application_sdk.testing.harness.outcome import Settled

        calls: list[dict[AssetRef, tuple[str, ...]]] = []

        async def _fake_read(
            _client: Any, _connection: str, ref_attributes: Any
        ) -> Any:
            calls.append({ref: tuple(attrs) for ref, attrs in ref_attributes.items()})
            return Settled(
                label="addressed",
                attempts=1,
                elapsed=timedelta(0),
                value=_addressed_right(),
            )

        monkeypatch.setattr(base_module.atlas, "read_asset_attributes", _fake_read)
        monkeypatch.setattr(
            base_module.atlas,
            "sample_asset_attributes",
            _settled({"Schema": [_sample(f"{_QN}/db/sch_empty", viewsCount=0)]}),
        )
        monkeypatch.setattr(base_module.atlas, "count_total_assets", _settled(2))
        monkeypatch.setattr(base_module.atlas, "count_assets", _settled({"Schema": 2}))

        outcome = await _suite(_FiveSchemas)._read_inventory(object(), _succeeded())

        assert calls == [
            {
                _ref("sch_empty"): ("viewsCount", "tableCount"),
                _ref("sch_busy"): ("viewsCount", "tableCount"),
            }
        ]
        assert outcome.addressed_attribute_reads == _addressed_right()

    async def test_a_suite_that_declares_nothing_makes_no_addressed_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.testing.e2e import base as base_module

        called = False

        async def _never(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal called
            called = True
            raise AssertionError("the addressed reader must not be called")

        monkeypatch.setattr(base_module.atlas, "read_asset_attributes", _never)
        monkeypatch.setattr(base_module.atlas, "count_total_assets", _settled(1))
        monkeypatch.setattr(base_module.atlas, "count_assets", _settled({"Schema": 1}))

        await _suite(_NoAttributeCheck)._read_inventory(object(), _succeeded())
        assert not called
