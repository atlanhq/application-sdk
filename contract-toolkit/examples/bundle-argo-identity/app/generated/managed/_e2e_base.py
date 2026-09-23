# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SQLAppE2ETest


class ManagedGeneratedE2EBase(SQLAppE2ETest):
    connector_short_name = "docstore"
    argo_package_name = "@atlan/docstore"
    argo_template_name = "atlan-docstore"
    app_service_url = "http://docstore.docstore-app.svc.cluster.local"
    connection_type = "docstore"
    connection_category = "database"
    connector_config_name = "atlan-connectors-docstore"
    # This entrypoint's own manifest. Without it the harness reads the
    # single-entrypoint default (app/generated/manifest.json), which a bundle
    # does not have, and fails with ManifestFileNotFoundError.
    manifest_path = "app/generated/managed/manifest.json"
    # Sent as metadata.entrypoint on the AE submit so Automation Engine
    # fetches THIS entrypoint's manifest from the deployed pod. A bare fetch
    # 404s "No manifest available" on a multi-entrypoint app.
    entrypoint = "managed"
    # Derived from this entrypoint's pipeline, so the assertions match the
    # DAG that was generated rather than the crawler-shaped SDK defaults.
    required_dag_nodes = ("extract", "publish")
    expect_connection = True
    require_nonempty_assets = True
    expect_lineage = False
