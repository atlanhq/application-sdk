"""LogCollector: kubectl-based log collection for K8s e2e tests."""

import asyncio
from pathlib import Path

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e.pods import get_pods

logger = get_logger(__name__)


async def _run_to_file(args: list[str], output_path: Path) -> None:
    """Run a command and write its stdout to a file (best-effort, never raises)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await proc.communicate()
        output_path.write_bytes(stdout_bytes)
    except Exception as exc:
        logger.warning("Log collection command failed (%s): %s", " ".join(args), exc)


class LogCollector:
    """Collect kubectl logs, pod descriptions, and events from a namespace.

    All collection methods are best-effort and never raise — matching the
    behaviour of the experimental-app-sdk's LogCollector.

    Args:
        namespace: K8s namespace to collect from.
        output_dir: Local directory to write collected files into.
    """

    def __init__(self, namespace: str, output_dir: Path) -> None:
        self.namespace = namespace
        self.output_dir = output_dir

    async def collect(self, label_selector: str = "") -> None:
        """Collect container logs and pod descriptions for all matching pods.

        Writes to :attr:`output_dir`:

        - ``{container}-{pod}.log`` — current container logs (tail 10 000 lines)
        - ``{container}-{pod}-previous.log`` — previous container logs (if any)
        - ``{pod}-describe.txt`` — ``kubectl describe pod``
        - ``pods-wide.txt`` — ``kubectl get pods -o wide``
        """
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("Failed to create output dir %s: %s", self.output_dir, exc)
            return

        pods = await get_pods(self.namespace, label_selector)

        tasks: list[asyncio.Task[None]] = []
        loop = asyncio.get_event_loop()

        for pod in pods:
            pod_name: str = pod.get("metadata", {}).get("name", "unknown")
            container_statuses = pod.get("status", {}).get("containerStatuses", [])

            # Describe each pod
            tasks.append(
                loop.create_task(
                    _run_to_file(
                        ["kubectl", "describe", "pod", pod_name, "-n", self.namespace],
                        self.output_dir / f"{pod_name}-describe.txt",
                    )
                )
            )

            for cs in container_statuses:
                container_name: str = cs.get("name", "unknown")
                safe_container = container_name.replace("/", "-")
                safe_pod = pod_name.replace("/", "-")
                prefix = f"{safe_container}-{safe_pod}"

                # Current logs
                tasks.append(
                    loop.create_task(
                        _run_to_file(
                            [
                                "kubectl",
                                "logs",
                                pod_name,
                                "-c",
                                container_name,
                                "-n",
                                self.namespace,
                                "--tail=10000",
                            ],
                            self.output_dir / f"{prefix}.log",
                        )
                    )
                )

                # Previous container logs (may not exist — ignore failure)
                if cs.get("restartCount", 0) > 0:
                    tasks.append(
                        loop.create_task(
                            _run_to_file(
                                [
                                    "kubectl",
                                    "logs",
                                    pod_name,
                                    "-c",
                                    container_name,
                                    "-n",
                                    self.namespace,
                                    "--previous",
                                    "--tail=10000",
                                ],
                                self.output_dir / f"{prefix}-previous.log",
                            )
                        )
                    )

        # Wide pod listing
        tasks.append(
            loop.create_task(
                _run_to_file(
                    ["kubectl", "get", "pods", "-n", self.namespace, "-o", "wide"],
                    self.output_dir / "pods-wide.txt",
                )
            )
        )

        if tasks:
            _results = await asyncio.gather(*tasks, return_exceptions=True)
            for _r in _results:
                if isinstance(_r, Exception):
                    logger.warning("Log collection task failed", exc_info=_r)

    async def collect_events(self) -> None:
        """Write namespace events sorted by timestamp to ``events.txt``."""
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("Failed to create output dir %s: %s", self.output_dir, exc)
            return

        await _run_to_file(
            [
                "kubectl",
                "get",
                "events",
                "-n",
                self.namespace,
                "--sort-by=.lastTimestamp",
            ],
            self.output_dir / "events.txt",
        )
