# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class SystemAppGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "system-app"
    argo_package_name = "@atlan/system-app"
    argo_template_name = "atlan-system-app"
    app_service_url = "http://system-app.system-app-app.svc.cluster.local"
