import platform
import re
import socket
import uuid

import psutil
from fastapi import APIRouter

from phoenix_sdk.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/system",
    tags=["health"],
    responses={404: {"description": "Not found"}},
)


@router.get("/health")
async def health():
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
    logger.info("Health")
    return info


@router.get("/ready")
async def ready():
    return {"status": "ok"}
