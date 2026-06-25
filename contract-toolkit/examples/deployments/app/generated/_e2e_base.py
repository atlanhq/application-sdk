# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class DeploymentsExampleGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "deployments-example"
    argo_package_name = "@atlan/deployments-example"
    argo_template_name = "atlan-deployments-example"
    app_service_url = "http://deployments-example.deployments-example-app.svc.cluster.local"
