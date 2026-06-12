"""Atlan Fleet Conformance — SARIF-profile schema, rule catalog, and disposition engine.

This package is **testing and conformance infrastructure only** — it is never imported
by production runtime code (``application_sdk``).  It ships alongside SDK releases so
that each SDK upgrade automatically carries the latest rule set to every consumer app.

Public API (import from ``suite.schema``)::

    from suite.schema import (
        # Disposition: the three-state outcome model
        Disposition,
        derive_disposition,

        # Rule catalog
        RuleDefinition,
        load_catalog,

        # SARIF report builder
        ReportBuilder,

        # Validation against the vendored SARIF 2.1.0 schema
        validate_sarif,
    )

See ``docs/schema-contract.md`` for the full mapping, evolution rules, and a
golden four-disposition example payload.
"""
