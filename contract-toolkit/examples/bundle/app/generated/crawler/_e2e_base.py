# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SQLAppE2ETest


class CrawlerGeneratedE2EBase(SQLAppE2ETest):
    connector_short_name = "crawler"
    argo_package_name = "@atlan/crawler"
    argo_template_name = "atlan-crawler"
    app_service_url = "http://crawler.crawler-app.svc.cluster.local"
    connection_type = "snowflake"
