"""Internal pyiceberg + pyarrow + Polaris-specific implementation.

Apps must not import from this package. The public surface
(``application_sdk.lakehouse.LakehouseReader`` / ``LakehouseWriter`` /
``EventsConsumer`` / ``EventAckWriter``) routes through here, and the
underlying implementation can change without breaking app code.
"""
