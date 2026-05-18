"""Internal: Polaris-server-specific catalog construction.

The Atlas data platform deploys Apache Polaris as the Iceberg REST Catalog
implementation. This package isolates Polaris-specific concerns:

  * URL pattern (``/api/polaris/api/catalog`` derived from ``ATLAN_DOMAIN_NAME``)
  * OAuth scope (``PRINCIPAL_ROLE:ALL``)
  * Vended-credentials wiring and the ``rest.sigv4-enabled`` quirk

If the platform ever swaps Polaris for another IRC implementation, only this
package changes — the format/ops code in ``_iceberg`` is unaffected.

Apps must not import from this package; use the public
``LakehouseReader.from_env`` / ``LakehouseWriter.from_env`` instead.
"""
