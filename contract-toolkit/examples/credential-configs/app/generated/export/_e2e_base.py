# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class ExportGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "credential-configs"
    argo_package_name = "@atlan/credential-configs"
    argo_template_name = "atlan-credential-configs"
    app_service_url = (
        "http://credential-configs.credential-configs-app.svc.cluster.local"
    )
    connection_type = "s3"
    connection_category = "ObjectStore"
    connector_config_name = "atlan-connectors-credential-configs"
    # This entrypoint's own manifest. Without it the harness reads the
    # single-entrypoint default (app/generated/manifest.json), which a bundle
    # does not have, and fails with ManifestFileNotFoundError.
    manifest_path = "app/generated/export/manifest.json"
    # Sent as metadata.entrypoint on the AE submit so Automation Engine
    # fetches THIS entrypoint's manifest from the deployed pod. A bare fetch
    # 404s "No manifest available" on a multi-entrypoint app.
    entrypoint = "export"
    # Derived from this entrypoint's pipeline, so the assertions match the
    # DAG that was generated rather than the crawler-shaped SDK defaults.
    required_dag_nodes = ("extract", "publish")
    expect_connection = True
    require_nonempty_assets = True
    expect_lineage = False
