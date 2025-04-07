import os
from enum import Enum


class ApplicationConstants(Enum):
    """
    Constants for the application
    """

    APPLICATION_NAME = os.getenv("ATLAN_APPLICATION_NAME", "default")
    TENANT_ID = os.getenv("ATLAN_TENANT_ID", "default")
    APP_DASHBOARD_HOST = str(os.getenv("ATLAN_APP_DASHBOARD_HOST", "localhost"))
    APP_DASHBOARD_PORT = int(os.getenv("ATLAN_APP_DASHBOARD_PORT", "8000"))

    APP_HOST = str(os.getenv("ATLAN_APP_HTTP_HOST", "localhost"))
    APP_PORT = int(os.getenv("ATLAN_APP_HTTP_PORT", "8000"))
