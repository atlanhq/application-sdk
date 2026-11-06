# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class AeActivitiesGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "ae-activities"
    argo_package_name = "@atlan/ae-activities"
    argo_template_name = "atlan-ae-activities"
    app_service_url = "http://ae-activities.ae-activities-app.svc.cluster.local"
