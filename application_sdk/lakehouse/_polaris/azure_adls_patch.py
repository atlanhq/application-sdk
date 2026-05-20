"""Internal: monkey-patch Daft's Iceberg IO converter for Azure ADLS.

Polaris vends Azure credentials with **account-scoped** keys, e.g.::

    adls.sas-token.<account>.dfs.core.windows.net
    adls.account-key.<account>.dfs.core.windows.net

Daft only checks the bare keys (``adls.sas-token`` / ``adls.account-key``),
so Polaris-vended Azure credentials are not picked up. This module
normalises scoped keys to bare keys before Daft's converter sees them.

Mirrors automation-engine-app's ``_patch_daft_adls_io`` exactly (kept as a
straight port; deviations would silently break cross-app compatibility).

The patch is idempotent and a no-op if Daft's Iceberg module isn't
importable. Apps don't call this directly — it's auto-applied by
``_polaris.catalog.load_catalog_from_env`` when ``CLOUD == "azure"``.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# adls.sas-token.<account>.dfs.core.windows.net
# adls.account-key.<account>.dfs.core.windows.net
_ADLS_SCOPED_KEY_RE = re.compile(
    r"^(adls\.(?:sas-token|account-key))\.([^.]+)\.dfs\.core\.windows\.net$"
)


def patch_daft_adls_io() -> None:
    """Patch Daft's Iceberg IO converter to normalise ADLS scoped keys.

    Idempotent. No-op if Daft's Iceberg module is not importable.
    """
    try:
        import daft.io.iceberg._iceberg as iceberg_mod
    except ImportError:
        return

    original_fn = getattr(
        iceberg_mod, "_convert_iceberg_file_io_properties_to_io_config", None
    )
    if original_fn is None or getattr(original_fn, "_adls_patched", False):
        return

    def _normalize(props: dict) -> dict:
        normalized = dict(props)
        for key, value in props.items():
            m = _ADLS_SCOPED_KEY_RE.match(key)
            if m:
                bare_key, account = m.group(1), m.group(2)
                if bare_key not in normalized:
                    normalized[bare_key] = value
                if "adls.account-name" not in normalized:
                    normalized["adls.account-name"] = account
        return normalized

    def _patched(props: dict):
        return original_fn(_normalize(props))

    _patched._adls_patched = True  # type: ignore[attr-defined]
    iceberg_mod._convert_iceberg_file_io_properties_to_io_config = _patched
    logger.info("Patched Daft Iceberg IO converter for ADLS scoped keys")
