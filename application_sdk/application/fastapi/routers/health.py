"""Router for handling health-related API endpoints.

This module provides endpoints for system health checks and readiness probes.
"""

import logging
import platform
import re
import socket
import uuid

import psutil
from fastapi import APIRouter

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))

router = APIRouter(
    prefix="/system",
    tags=["health"],
    responses={404: {"description": "Not found"}},
)


@router.get("/health")
async def health():
    """Get system health information.

    This endpoint provides detailed information about the system's hardware
    and software configuration, including platform details, network information,
    and resource statistics.

    Returns:
        dict: A dictionary containing system information with the following keys:
            - platform (str): Operating system name
            - platform_release (str): Operating system release version
            - platform_version (str): Operating system version details
            - architecture (str): System architecture
            - hostname (str): System hostname
            - ip_address (str): System IP address
            - mac_address (str): System MAC address
            - processor (str): Processor information
            - ram (str): Total RAM in GB
    """
    info = {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "hostname": socket.gethostname(),
        "ip_address": socket.gethostbyname(socket.gethostname()),
        "mac_address": ":".join(re.findall("..", "%012x" % uuid.getnode())),
        "processor": platform.processor(),
        "ram": str(round(psutil.virtual_memory().total / (1024.0**3))) + " GB",
    }
    logger.info("Health check passed")
    return info


@router.get("/ready")
async def ready():
    """Check system readiness.

    This endpoint provides a simple readiness probe to verify if the system
    is ready to handle requests.

    Returns:
        dict: A dictionary containing the system status:
            - status (str): Always "ok" if the system is ready
    """
    return {"status": "ok"}


def get_health_router() -> APIRouter:
    """Get the health check router.

    Returns:
        APIRouter: FastAPI router containing health check endpoints.
    """
    return router
