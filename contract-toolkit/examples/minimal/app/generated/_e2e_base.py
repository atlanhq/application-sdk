# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class MinimalGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "minimal"
    argo_package_name = "@atlan/minimal"
    argo_template_name = "atlan-minimal"
    app_service_url = "http://minimal.minimal-app.svc.cluster.local"
