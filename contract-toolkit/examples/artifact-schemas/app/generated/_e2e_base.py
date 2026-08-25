# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class ArtifactSchemasExampleGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "artifact-schemas-example"
    argo_package_name = "@atlan/artifact-schemas-example"
    argo_template_name = "atlan-artifact-schemas-example"
    app_service_url = (
        "http://artifact-schemas-example.artifact-schemas-example-app.svc.cluster.local"
    )
