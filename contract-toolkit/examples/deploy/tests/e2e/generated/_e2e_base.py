# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class DeployGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "deploy"
    argo_package_name = "@atlan/deploy"
    argo_template_name = "atlan-deploy"
    app_service_url = "http://deploy.deploy-app.svc.cluster.local"
