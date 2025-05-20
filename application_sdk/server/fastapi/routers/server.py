"""Router for handling server-related API endpoints.

Note:
    * The shutdown endpoint is currently used only by the Argo compatible workflow to stop the application
        and exit with a status code of 0 after checking the status of the workflow.
    * The force flag is used to force the application to stop immediately.
"""

import asyncio
import os
import platform
import re
import socket
import threading
import time
import uuid

import psutil
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Create a router instance using the alias
router = APIRouter(
    prefix="/server",
    tags=["server"],
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


def terminate_process():
    """
    Terminate the process by sending a SIGTERM signal to the parent process.
    """
    time.sleep(1)
    parent = psutil.Process(psutil.Process(os.getpid()).ppid())
    parent.terminate()


@router.post("/shutdown")
async def shutdown(force: bool = False):
    """
    Stop the application.
    Args:
        force(optional): Whether to force the application to stop immediately
    """
    logger.info("Stopping application")

    async def shutdown():
        # Wait for existing requests to complete
        await asyncio.sleep(2)

        if force:
            logger.info("Force shutting down application")
        else:
            # Get all running asyncio tasks except the current one
            pending_tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ]
            logger.info(f"Cancelling {len(pending_tasks)} tasks")

            # Send cancel signal to all pending tasks
            for task in pending_tasks:
                task.cancel()

            try:
                # Wait for tasks to complete with a timeout
                await asyncio.wait(pending_tasks, timeout=10)
            except asyncio.CancelledError:
                logger.info("Tasks cancelled successfully")
            except Exception as e:
                logger.error(f"Error during task cancellation: {e}")

    # TO BE IMPLEMENTED - currently graceful shutdown is dysfunctional
    # asyncio.create_task(shutdown())

    threading.Thread(target=terminate_process, daemon=True).start()

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


def get_server_router() -> APIRouter:
    return router
