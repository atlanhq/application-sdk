"""AppDeployer: helm-based deploy/undeploy for K8s e2e tests."""

import asyncio
from pathlib import Path

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e.config import AppConfig

logger = get_logger(__name__)

# Path to the bundled Helm chart (relative to this file's package root).
_HELM_CHART_PATH = Path(__file__).parents[3] / "helm" / "atlan-app"


class DeploymentError(Exception):
    """Raised when a helm or kubectl command fails."""


def _parse_image(image: str) -> dict[str, str]:
    """Parse a full image reference into helm set values.

    Examples::

        >>> _parse_image("ghcr.io/atlanhq/my-app:v1.0.0")
        {"image.registry": "ghcr.io", "image.repository": "atlanhq/my-app", "image.tag": "v1.0.0"}

        >>> _parse_image("localhost/my-app:e2e")
        {"image.registry": "localhost", "image.repository": "my-app", "image.tag": "e2e"}
    """
    tag = "latest"
    # Split tag from the last component only (not from registry port like localhost:5000)
    last_slash_idx = image.rfind("/")
    last_part = image[last_slash_idx + 1 :]
    if ":" in last_part:
        colon_idx = image.rfind(":")
        tag = image[colon_idx + 1 :]
        img_no_tag = image[:colon_idx]
    else:
        img_no_tag = image

    parts = img_no_tag.split("/", 1)
    first = parts[0]
    if len(parts) > 1 and ("." in first or ":" in first or first == "localhost"):
        registry = first
        repository = parts[1]
    else:
        registry = ""
        repository = img_no_tag

    values: dict[str, str] = {"image.repository": repository, "image.tag": tag}
    if registry:
        values["image.registry"] = registry
    return values


async def _run(args: list[str], check: bool = True) -> tuple[int, str, str]:
    """Run a subprocess command, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    if check and proc.returncode != 0:
        cmd_str = " ".join(args)
        raise DeploymentError(
            f"Command failed (exit {proc.returncode}): {cmd_str}\n{stderr}"
        )
    return proc.returncode or 0, stdout, stderr


class AppDeployer:
    """Deploy and manage a single app in K8s via Helm for e2e testing."""

    def __init__(self, config: AppConfig, chart_path: Path | None = None) -> None:
        self.config = config
        self._chart_path = chart_path or _HELM_CHART_PATH

    def _helm_set_args(self) -> list[str]:
        """Build the list of ``--set key=value`` args for helm."""
        values: dict[str, str] = {
            "appName": self.config.app_name,
            "appModule": self.config.app_module,
            "namespaceOverride": self.config.namespace,
        }
        values.update(_parse_image(self.config.image))
        # User-supplied overrides win.
        values.update(self.config.helm_values)

        args: list[str] = []
        for k, v in values.items():
            args += ["--set", f"{k}={v}"]
        return args

    async def deploy(self) -> None:
        """Run ``helm upgrade --install --wait`` to deploy the app."""
        cmd = [
            "helm",
            "upgrade",
            "--install",
            "--wait",
            f"--timeout={self.config.timeout}s",
            "--namespace",
            self.config.namespace,
            "--create-namespace",
            self.config.app_name,
            str(self._chart_path),
        ] + self._helm_set_args()

        logger.info(
            "Deploying %s to namespace %s", self.config.app_name, self.config.namespace
        )
        await _run(cmd)
        logger.info("Deployed %s", self.config.app_name)

    async def undeploy(self) -> None:
        """Uninstall the Helm release and delete the namespace."""
        logger.info("Undeploying %s", self.config.app_name)
        try:
            await _run(
                [
                    "helm",
                    "uninstall",
                    self.config.app_name,
                    "--namespace",
                    self.config.namespace,
                ]
            )
        except DeploymentError as exc:
            logger.warning("helm uninstall failed (ignored): %s", exc)
        try:
            await _run(
                [
                    "kubectl",
                    "delete",
                    "namespace",
                    self.config.namespace,
                    "--ignore-not-found",
                ]
            )
        except DeploymentError as exc:
            logger.warning("kubectl delete namespace failed (ignored): %s", exc)

    async def is_ready(self) -> bool:
        """Return True if all pods for this app are Ready."""
        code, stdout, _ = await _run(
            [
                "kubectl",
                "get",
                "pods",
                "-n",
                self.config.namespace,
                "-l",
                f"app={self.config.app_name}",
                "--field-selector=status.phase=Running",
                "-o",
                "jsonpath={.items[*].status.containerStatuses[*].ready}",
            ],
            check=False,
        )
        if code != 0:
            return False
        # All entries must be "true"
        ready_values = stdout.strip().split()
        return bool(ready_values) and all(v == "true" for v in ready_values)

    async def wait_ready(self, poll_interval: float = 5.0) -> None:
        """Poll :meth:`is_ready` until all pods are Ready or timeout elapses."""
        import time

        deadline = time.monotonic() + self.config.timeout
        while time.monotonic() < deadline:
            if await self.is_ready():
                logger.info("%s is ready", self.config.app_name)
                return
            await asyncio.sleep(poll_interval)
        raise DeploymentError(
            f"{self.config.app_name} did not become ready within {self.config.timeout}s"
        )
