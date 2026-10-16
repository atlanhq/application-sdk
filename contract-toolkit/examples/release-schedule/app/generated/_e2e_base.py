# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class ReleaseScheduleGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "release-schedule"
    argo_package_name = "@atlan/release-schedule"
    argo_template_name = "atlan-release-schedule"
    app_service_url = "http://release-schedule.release-schedule-app.svc.cluster.local"
