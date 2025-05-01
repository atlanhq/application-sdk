import os
from enum import Enum


class TemporalConstants(Enum):
    HOST = os.getenv("ATLAN_TEMPORAL_HOST", "localhost")
    PORT = os.getenv("ATLAN_TEMPORAL_PORT", "7233")
    NAMESPACE = os.getenv("ATLAN_TEMPORAL_NAMESPACE", "default")
    APPLICATION_NAME = os.getenv("ATLAN_TEMPORAL_APPLICATION_NAME", "default")
