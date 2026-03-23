"""Pod status and log helpers for K8s e2e tests."""

import asyncio
import json
import time

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


async def _run(args: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
    )


async def get_pods(namespace: str, label_selector: str = "") -> list[dict]:  # type: ignore[type-arg]
    """Return pod objects from ``kubectl get pods -o json``."""
    cmd = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
    if label_selector:
        cmd += ["-l", label_selector]
    code, stdout, stderr = await _run(cmd)
    if code != 0:
        logger.warning("get_pods failed: %s", stderr)
        return []
    try:
        return json.loads(stdout).get("items", [])
    except json.JSONDecodeError:
        logger.warning("get_pods: failed to parse JSON output")
        return []


async def wait_for_pods_ready(
    namespace: str,
    label_selector: str,
    timeout: int = 120,
    poll_interval: float = 5.0,
) -> None:
    """Wait until all matching pods have all containers Ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pods = await get_pods(namespace, label_selector)
        if pods:
            all_ready = all(
                all(
                    cs.get("ready", False)
                    for cs in pod.get("status", {}).get("containerStatuses", [])
                )
                for pod in pods
            )
            if all_ready:
                return
        await asyncio.sleep(poll_interval)
    raise TimeoutError(
        f"Pods with selector '{label_selector}' in namespace '{namespace}' "
        f"did not become ready within {timeout}s"
    )


async def get_pod_logs(namespace: str, pod_name: str, container: str = "") -> str:
    """Return stdout/stderr logs for a pod (or specific container)."""
    cmd = ["kubectl", "logs", pod_name, "-n", namespace]
    if container:
        cmd += ["-c", container]
    code, stdout, stderr = await _run(cmd)
    if code != 0:
        logger.warning("get_pod_logs failed for %s: %s", pod_name, stderr)
        return ""
    return stdout
