# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class DeployNoEmitGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "deploy-no-emit"
    argo_package_name = "@atlan/deploy-no-emit"
    argo_template_name = "atlan-deploy-no-emit"
    app_service_url = "http://deploy-no-emit.deploy-no-emit-app.svc.cluster.local"
