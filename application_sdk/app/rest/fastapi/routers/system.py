"""Router for handling health-related API endpoints."""

import asyncio
import logging
import os
import platform
import re
import signal
import socket
import uuid

import psutil
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))

router = APIRouter(
    prefix="/system",
    tags=["health"],
    responses={404: {"description": "Not found"}},
)


def get_ip_address():
    """Get IP address with fallback options."""
    try:
        # Try getting IP using hostname first
        return socket.gethostbyname(socket.gethostname())
    except socket.gaierror:
        try:
            # Fallback: Create a socket connection to an external server
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # We don't actually connect, just start the process
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"  # Return localhost if all else fails


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
        "ip_address": get_ip_address(),
        "mac_address": ":".join(re.findall("..", "%012x" % uuid.getnode())),
        "processor": platform.processor(),
        "ram": str(round(psutil.virtual_memory().total / (1024.0**3))) + " GB",
    }
    logger.info("Health check passed")
    return info


@router.post("/shutdown")
async def shutdown(force: bool = False):
    """
    Stop the application.
    Args:
        force(optional): Whether to force the application to stop
    """
    logger.info("Stopping application")

    async def shutdown():
        # Wait for existing requests to complete
        await asyncio.sleep(2)

        if force:
            logger.info("Force shutting down application")
        else:
            # Get all running tasks except the current one
            pending_tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ]
            logger.info(f"Waiting for {len(pending_tasks)} tasks to complete")
            # Wait for all pending tasks to complete

            await asyncio.gather(*pending_tasks, return_exceptions=True)

        # Stop the server gracefully
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(shutdown())

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": "Application shutting down",
            "force": force,
        },
    )


@router.get("/ready")
async def ready():
    """
    Check if the system is ready.

    :return: A dictionary containing the status "ok".
    """
    return {"status": "ok"}


def get_health_router() -> APIRouter:
    return router
