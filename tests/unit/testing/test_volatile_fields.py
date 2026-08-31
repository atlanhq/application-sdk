"""Unit tests for the canonical comparison field sets.

The literal-set assertions here are deliberate regression locks. Both existing
constants are now composed from these sets, and both are consumed by code that
runs in production, so silent drift in either direction must fail loudly.
"""

from application_sdk.testing.integration.comparison import (
    DEFAULT_IGNORED_FIELDS,
    DEFAULT_IGNORED_NESTED_FIELDS,
)
from application_sdk.testing.parity.comparator import VOLATILE_FIELDS
from application_sdk.testing.volatile_fields import (
    ENVIRONMENT_SCOPED_FIELDS,
    ENVIRONMENT_SCOPED_NESTED_FIELDS,
    RUN_VOLATILE_FIELDS,
)


class TestCanonicalSets:
    def test_run_volatile_is_exactly_the_three_run_scoped_keys(self):
        assert RUN_VOLATILE_FIELDS == {
            "lastSyncRun",
            "lastSyncRunAt",
            "lastSyncWorkflowName",
        }

    def test_environment_scoped_contents(self):
        assert ENVIRONMENT_SCOPED_FIELDS == {
            "connectionName",
            "connectionQualifiedName",
            "databaseQualifiedName",
            "qualifiedName",
            "schemaQualifiedName",
            "tableQualifiedName",
            "tenantId",
            "viewQualifiedName",
        }

    def test_environment_scoped_nested_contents(self):
        assert ENVIRONMENT_SCOPED_NESTED_FIELDS == {
            "atlanSchema",
            "database",
            "materialisedView",
            "parentTable",
            "table",
            "tablePartition",
            "view",
        }

    def test_concepts_are_disjoint(self):
        assert not RUN_VOLATILE_FIELDS & ENVIRONMENT_SCOPED_FIELDS
        assert not ENVIRONMENT_SCOPED_FIELDS & ENVIRONMENT_SCOPED_NESTED_FIELDS

    def test_qualified_name_is_environment_scoped_not_run_volatile(self):
        """The distinction FND-819 exists to make.

        A golden diff keys on qualifiedName, so it must not be classified as
        run-volatile or the golden assertion would strip its own join key.
        """
        assert "qualifiedName" in ENVIRONMENT_SCOPED_FIELDS
        assert "qualifiedName" not in RUN_VOLATILE_FIELDS


class TestProductionConstantsUnchanged:
    """Locks the two live constants to the exact contents they shipped with."""

    def test_default_ignored_fields_is_the_same_eleven(self):
        assert DEFAULT_IGNORED_FIELDS == {
            "qualifiedName",
            "connectionQualifiedName",
            "lastSyncWorkflowName",
            "lastSyncRun",
            "lastSyncRunAt",
            "tenantId",
            "connectionName",
            "databaseQualifiedName",
            "schemaQualifiedName",
            "tableQualifiedName",
            "viewQualifiedName",
        }
        assert len(DEFAULT_IGNORED_FIELDS) == 11

    def test_default_ignored_nested_fields_is_the_same_seven(self):
        assert DEFAULT_IGNORED_NESTED_FIELDS == {
            "atlanSchema",
            "database",
            "table",
            "view",
            "materialisedView",
            "parentTable",
            "tablePartition",
        }
        assert len(DEFAULT_IGNORED_NESTED_FIELDS) == 7

    def test_parity_volatile_fields_is_the_same_three(self):
        assert VOLATILE_FIELDS == {
            "lastSyncWorkflowName",
            "lastSyncRun",
            "lastSyncRunAt",
        }
        assert len(VOLATILE_FIELDS) == 3

    def test_default_ignored_is_the_union_of_both_concepts(self):
        assert DEFAULT_IGNORED_FIELDS == RUN_VOLATILE_FIELDS | ENVIRONMENT_SCOPED_FIELDS

    def test_parity_set_is_a_strict_subset_of_the_integration_set(self):
        assert VOLATILE_FIELDS < DEFAULT_IGNORED_FIELDS

    def test_live_constants_are_mutable_sets(self):
        """Callers may copy/union these; the public type must not become frozen."""
        assert isinstance(DEFAULT_IGNORED_FIELDS, set)
        assert isinstance(DEFAULT_IGNORED_NESTED_FIELDS, set)
        assert isinstance(VOLATILE_FIELDS, set)

    def test_mutating_a_live_constant_cannot_corrupt_the_canonical_set(self):
        copied = set(DEFAULT_IGNORED_FIELDS)
        copied.add("injected")
        assert "injected" not in RUN_VOLATILE_FIELDS
        assert "injected" not in ENVIRONMENT_SCOPED_FIELDS
