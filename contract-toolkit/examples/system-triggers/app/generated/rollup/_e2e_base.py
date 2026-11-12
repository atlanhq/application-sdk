# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SystemAppE2ETest


class RollupGeneratedE2EBase(SystemAppE2ETest):
    connector_short_name = "system-triggers"
    # This entrypoint's own manifest. Without it the harness reads the
    # single-entrypoint default (app/generated/manifest.json), which a bundle
    # does not have, and fails with ManifestFileNotFoundError.
    manifest_path = "app/generated/rollup/manifest.json"
    # Sent as metadata.entrypoint on the AE submit so Automation Engine
    # fetches THIS entrypoint's manifest from the deployed pod. A bare fetch
    # 404s "No manifest available" on a multi-entrypoint app.
    entrypoint = "rollup"
    # Derived from this entrypoint's pipeline, so the assertions match the
    # DAG that was generated rather than the crawler-shaped SDK defaults.
    required_dag_nodes = ("extract",)
    expect_connection = False
    require_nonempty_assets = False
    expect_lineage = False
