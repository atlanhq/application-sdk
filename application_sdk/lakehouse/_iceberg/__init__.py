"""Internal: Iceberg-format-specific operations.

Generic across IRC (Iceberg REST Catalog) implementations — this is where
``pyiceberg`` and ``pyarrow`` live. Polaris-specific connection details are
isolated in the sibling :mod:`application_sdk.lakehouse._polaris` package.

Apps must not import from this package; the public surface
(``LakehouseReader`` / ``LakehouseWriter`` / ``events_read`` /
``events_ack`` / ``Schema`` / ``Field`` / ``PartitionBy``) is the only
supported entry point.
"""
