import os
from enum import Enum


class ApplicationConstants(Enum):
    """
    Constants for the application
    """

    APPLICATION_NAME = os.getenv("ATLAN_APPLICATION_NAME", "default")
    TENANT_ID = os.getenv("ATLAN_TENANT_ID", "default")
