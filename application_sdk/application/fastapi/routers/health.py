"""Router for handling health-related API endpoints."""

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
    """
    Check the health of the system.

    :return: A dictionary containing system information.
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
    """
    Check if the system is ready.

    :return: A dictionary containing the status "ok".
    """
    return {"status": "ok"}


def get_health_router() -> APIRouter:
    return router
