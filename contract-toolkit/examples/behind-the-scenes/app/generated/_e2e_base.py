# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class BehindTheScenesGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "behind-the-scenes"
    argo_package_name = "@atlan/behind-the-scenes"
    argo_template_name = "atlan-behind-the-scenes"
    app_service_url = "http://behind-the-scenes.behind-the-scenes-app.svc.cluster.local"
