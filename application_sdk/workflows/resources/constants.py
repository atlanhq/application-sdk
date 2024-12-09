import os
from enum import Enum


class TemporalConstants(Enum):
    HOST = os.getenv("TEMPORAL_HOST", "127.0.0.1")
    PORT = os.getenv("TEMPORAL_PORT", "7233")
    NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
    APPLICATION_NAME = os.getenv("TEMPORAL_APPLICATION_NAME", "default")