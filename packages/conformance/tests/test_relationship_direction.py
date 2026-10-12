"""Tests for P055 OneToManyLinkFromParent and its baked relationship table (FND-3490).

The check is exercised through the P-series ``scan_text`` so the real wiring
(import collection, suppression handling) is covered, not just the detector.
The table tests read the committed JSON — the same file a consumer-repo scan
reads — and the drift test rebuilds it from the installed pyatlan_v9.
"""

from __future__ import annotations

import textwrap

from conformance.suite.checks.prescriptions import scan_text as p_scan
from conformance.suite.checks.prescriptions._relationship_directions import (
    DATA_PATH,
    SetEnd,
    build_relationship_data,
    load_relationship_data,
    serialize,
)
from conformance.suite.schema.findings import Finding


def _p055(src: str) -> list[Finding]:
    return [
        f for f in p_scan(textwrap.dedent(src), "app/mapper.py") if f.rule_id == "P055"
    ]


def _unsuppressed(src: str) -> list[Finding]:
    return [f for f in _p055(src) if not f.suppressed]


# ── Table ────────────────────────────────────────────────────────────────────────


def test_committed_table_matches_pinned_pyatlan() -> None:
    # No importorskip: the [test] extra installs the SDK, which depends on
    # pyatlan, so a missing pyatlan_v9 must fail here rather than let a stale
    # table pass unnoticed.
    assert DATA_PATH.read_text(encoding="utf-8") == serialize(
        build_relationship_data()
    ), (
        "relationship_directions.json is stale — run "
        "`uv run --directory packages/conformance --extra test atlan-application-sdk-conformance gen-relationship-directions`."
    )


def test_table_holds_the_fabric_list_end() -> None:
    assert load_relationship_data().set_ends["Process"]["fabric_activities"] == SetEnd(
        attribute="fabricActivities",
        target_type="FabricActivity",
        inverse="fabric_process",
        inverse_attribute="fabricProcess",
    )


def test_table_holds_the_sql_hierarchy() -> None:
    table = load_relationship_data().set_ends

    assert table["Table"]["columns"].inverse == "table"
    assert table["Schema"]["tables"].inverse == "atlan_schema"
    # Ambiguous by types alone (Database also has the many-to-many sql_schemas):
    # supplied from the typedefs.
    assert table["Database"]["schemas"].inverse == "database"


def test_table_omits_many_to_many_and_single_ends() -> None:
    table = load_relationship_data().set_ends

    assert "inputs" not in table["Process"]
    assert "outputs" not in table["Process"]
    assert "sql_schemas" not in table["Database"]
    assert "table" not in table.get("Column", {})


def test_table_applies_an_end_to_multiply_inherited_subtypes() -> None:
    """DbtProcess is a Dbt and a Process; the Related MRO shows only Dbt."""
    table = load_relationship_data().set_ends

    assert (
        table["DbtProcess"]["fabric_activities"]
        == table["Process"]["fabric_activities"]
    )


def test_table_does_not_borrow_an_end_from_a_same_named_list() -> None:
    """KafkaConsumerGroup.kafka_topics is many-to-many; only KafkaCluster's is 1-to-N."""
    table = load_relationship_data().set_ends

    assert table["KafkaCluster"]["kafka_topics"].inverse == "kafka_cluster"
    assert "kafka_topics" not in table.get("KafkaConsumerGroup", {})


def test_table_applies_a_supertype_end_to_subtypes() -> None:
    table = load_relationship_data().set_ends

    assert table["Table"]["links"] == table["Column"]["links"]
    assert table["Column"]["links"].target_type == "Link"


# ── Fires ────────────────────────────────────────────────────────────────────────


def test_fires_on_attribute_assignment_to_constructed_parent() -> None:
    findings = _unsuppressed(
        """
        from pyatlan_v9.model.assets import Process, RelatedFabricActivity

        def map_process(qn: str, activity_qn: str) -> Process:
            process = Process(qualified_name=qn)
            process.fabric_activities = [RelatedFabricActivity(qualified_name=activity_qn)]
            return process
        """
    )

    assert len(findings) == 1
    assert findings[0].line == 6
    assert "Process.fabric_activities" in findings[0].message
    assert "FabricActivity.fabric_process" in findings[0].message


def test_fires_on_constructor_keyword() -> None:
    assert _unsuppressed(
        """
        from pyatlan_v9.model.assets import Table

        def map_table(qn: str, cols: list) -> Table:
            return Table(qualified_name=qn, columns=cols)
        """
    )


def test_fires_on_creator_keyword() -> None:
    assert _unsuppressed(
        """
        from pyatlan_v9.model.assets import Table

        def map_table(cols: list) -> Table:
            return Table.creator(name="t", schema_qualified_name="s", columns=cols)
        """
    )


def test_fires_inside_lambda() -> None:
    assert _unsuppressed(
        """
        from pyatlan_v9.model.assets import Table

        build = lambda cols: Table(columns=cols)
        """
    )


def test_fires_on_append_through_annotated_parameter() -> None:
    assert _unsuppressed(
        """
        from pyatlan_v9.model.assets import Schema

        def attach(schema: Schema, table) -> None:
            schema.tables.append(table)
        """
    )


def test_fires_on_augmented_assignment() -> None:
    assert _unsuppressed(
        """
        from pyatlan_v9.model.assets import Table

        t = Table(qualified_name="q")
        t.columns += extra
        """
    )


def test_fires_through_module_alias() -> None:
    assert _unsuppressed(
        """
        import pyatlan_v9.model.assets as A

        def build():
            db = A.Database(qualified_name="q")
            db.schemas = [A.RelatedSchema(qualified_name="q/s")]
        """
    )


def test_fires_by_value_when_receiver_is_unresolved() -> None:
    findings = _unsuppressed(
        """
        from pyatlan_v9.model.assets import RelatedFabricActivity

        class Mapper:
            def link(self, activity_qn: str) -> None:
                self.process.fabric_activities = [
                    RelatedFabricActivity(qualified_name=activity_qn)
                ]
        """
    )

    assert len(findings) == 1
    # Process and FabricDataPipeline both carry this list end with different
    # child references, so the message offers both.
    assert findings[0].message.startswith("fabric_activities on the parent")
    assert "FabricActivity.fabric_process" in findings[0].message
    assert "FabricActivity.fabric_data_pipeline" in findings[0].message


def test_fires_by_value_with_ref_by_qualified_name() -> None:
    assert _unsuppressed(
        """
        from pyatlan_v9.model.assets import FabricActivity

        def link(parent, qns):
            parent.fabric_activities = [
                FabricActivity.ref_by_qualified_name(qn) for qn in qns
            ]
        """
    )


def test_by_value_names_no_owner_when_owners_share_the_end() -> None:
    findings = _unsuppressed(
        """
        from pyatlan_v9.model.assets import RelatedLink

        def link(asset, qn):
            asset.links = [RelatedLink(qualified_name=qn)]
        """
    )

    assert len(findings) == 1
    assert findings[0].message.startswith("links on the parent")


# ── Silent ───────────────────────────────────────────────────────────────────────


def test_silent_on_child_side_reference() -> None:
    """The compliant shape: each child references its single parent."""
    assert not _p055(
        """
        from pyatlan_v9.model.assets import FabricActivity, RelatedProcess

        def map_activity(qn: str, process_qn: str) -> FabricActivity:
            activity = FabricActivity(qualified_name=qn)
            activity.fabric_process = RelatedProcess(qualified_name=process_qn)
            return activity
        """
    )


def test_silent_on_many_to_many_end() -> None:
    assert not _p055(
        """
        from pyatlan_v9.model.assets import Process, RelatedTable

        def map_process(qn: str, src: str) -> Process:
            process = Process(qualified_name=qn, inputs=[RelatedTable(qualified_name=src)])
            process.outputs = [RelatedTable(qualified_name=src)]
            return process
        """
    )


def test_silent_on_none() -> None:
    assert not _p055(
        """
        from pyatlan_v9.model.assets import Table

        t = Table(qualified_name="q", columns=None)
        t.columns = None
        """
    )


def test_silent_on_empty_list() -> None:
    assert not _p055(
        """
        from pyatlan_v9.model.assets import Table

        t = Table(qualified_name="q", columns=[])
        t.columns = []
        t.columns = list()
        t.columns.extend([])
        """
    )


def test_silent_when_module_alias_is_shadowed_by_parameter() -> None:
    assert not _p055(
        """
        import pyatlan_v9.model.assets as assets

        def build(assets, cols):
            return assets.Table(columns=cols)
        """
    )


def test_silent_when_module_alias_is_reassigned() -> None:
    assert not _p055(
        """
        import pyatlan_v9.model.assets as assets
        import other_models

        assets = other_models
        t = assets.Table(columns=cols)
        """
    )


def test_silent_when_enclosing_function_shadows_class_name() -> None:
    assert not _p055(
        """
        from pyatlan_v9.model.assets import Table

        def outer(Table):
            def inner(cols):
                return Table(columns=cols)
            return inner
        """
    )


def test_class_body_binding_does_not_shadow_methods() -> None:
    """A class attribute named like an import is not visible inside methods."""
    assert _unsuppressed(
        """
        from pyatlan_v9.model.assets import Table

        class Mapper:
            Table = None

            def build(self, cols):
                return Table(columns=cols)
        """
    )


def test_silent_without_pyatlan_v9_import() -> None:
    assert not _p055(
        """
        import pandas as pd

        df = pd.DataFrame()
        df.columns = ["a", "b"]
        """
    )


def test_silent_on_legacy_pyatlan() -> None:
    """Legacy pyatlan is O004's concern; P055 covers pyatlan_v9 mappers only."""
    assert not _p055(
        """
        from pyatlan.model.assets import Table

        t = Table()
        t.columns = cols
        """
    )


def test_silent_on_unresolved_receiver_without_value_evidence() -> None:
    assert not _p055(
        """
        from pyatlan_v9.model.assets import Table

        def f(df, cols):
            df.columns = cols
        """
    )


def test_silent_by_value_when_field_is_many_to_many_elsewhere() -> None:
    """``reports`` is 1-to-N on SalesforceOrganization but many-to-many on
    SalesforceDashboard; an unresolved receiver could be either."""
    assert not _p055(
        """
        from pyatlan_v9.model.assets import RelatedSalesforceReport, SalesforceDashboard

        def map_dashboard(r, org_qn):
            asset = _new(SalesforceDashboard, name=r["name"])
            asset.reports = [RelatedSalesforceReport(qualified_name=org_qn + "/r")]
            return asset
        """
    )


def test_other_list_fields_holds_a_field_that_is_not_always_one_to_many() -> None:
    data = load_relationship_data()

    assert "reports" in data.set_ends["SalesforceOrganization"]
    assert "reports" in data.other_list_fields
    assert "fabric_activities" not in data.other_list_fields


def test_silent_when_name_is_rebound_to_another_type() -> None:
    assert not _p055(
        """
        from pyatlan_v9.model.assets import Table, View

        def f(flag, cols):
            x = Table(qualified_name="t")
            if flag:
                x = make_other()
            x.columns = cols
        """
    )


def test_inner_function_scope_does_not_leak() -> None:
    assert not _p055(
        """
        from pyatlan_v9.model.assets import Table

        def outer(cols):
            t = Table(qualified_name="t")

            def inner(t):
                t.columns = cols

            return t, inner
        """
    )


def test_inline_directive_suppresses() -> None:
    findings = _p055(
        """
        from pyatlan_v9.model.assets import Table

        t = Table(qualified_name="q")
        t.columns = cols  # conformance: ignore[P055] comparison value, never published
        """
    )

    assert len(findings) == 1
    assert findings[0].suppressed
