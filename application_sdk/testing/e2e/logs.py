"""LogCollector: evidence collection from a namespace for K8s e2e tests.

The pod listing and the container-log reads now go through the typed cluster
reader (FND-241) rather than through ``kubectl`` subprocesses — ``get_pods`` and
``get_pod_logs`` are deleted, not wrapped. The written file layout is unchanged:
one ``{container}-{pod}.log`` per container, a ``-previous.log`` beside it after a
restart, one ``{pod}-describe.txt`` per pod.

Three of the four artefacts still shell out to ``kubectl``: ``describe pod``,
``get pods -o wide`` and ``get events``. They are human-readable renderings with
no typed equivalent — ``describe`` in particular is a formatter, not an endpoint —
and their whole value is that a person reading a red CI leg sees exactly what they
would have seen at a terminal. Replacing them with sanitized JSON would be a
different artefact wearing the same filename.

All three pin the reader's ``kube_context``. An evidence bundle that describes a
different cluster from the one the reads came from is worse than no bundle: it
is wrong in a way nothing flags, because ``kubectl`` against the current context
succeeds perfectly well — it just answers about somewhere else.

**This collector is allowed to fail open, and it is the only thing here that
is.** The ban on empty-result-on-error (FND-224's C4) is about readings that get
*graded*: an unreadable count must not be scored as a low count. An evidence dump
is never graded — it is what someone reads *after* the verdict — so a collector
that raised would turn a diagnosable failure into an undiagnosable one. Every
failure is logged, and the rest of the collection still runs.

Deprecated (removed in v4.0) alongside the rest of this module's K8s surface;
evidence collection lands properly in
:mod:`application_sdk.testing.harness.evidence`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.cluster import PodState
from application_sdk.testing.harness.cluster._portforward import kubectl_argv
from application_sdk.testing.harness.cluster.kube import KubernetesReader

logger = get_logger(__name__)

#: Matches the ``--tail`` the ``kubectl`` version used, so the artefacts a
#: reviewer compares across runs stay the same size.
_TAIL_LINES = 10_000


async def _run_to_file(args: Sequence[str], output_path: Path) -> None:
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
        logger.warning(
            "Log collection command failed (%s) error_type=%s",
            " ".join(args),
            type(exc).__name__,
            exc_info=True,
        )


class LogCollector:
    """Collect container logs, pod descriptions, and events from a namespace.

    All collection methods are best-effort and never raise — see the module
    docstring for why that is the right call here and nowhere else.

    Args:
        namespace: K8s namespace to collect from.
        output_dir: Local directory to write collected files into.
        reader: Cluster reader to list pods and read their logs with. Defaults to
            a :class:`~application_sdk.testing.harness.cluster.KubernetesReader`
            over the ambient kubeconfig. Typed as the concrete reader rather than
            as :class:`~application_sdk.testing.harness.cluster.ClusterReader`
            because it needs two things that Protocol deliberately does not
            carry: a single container's output, and the *previous* container's
            output after a restart — which is where a crash loop's actual cause
            is.
    """

    def __init__(
        self,
        namespace: str,
        output_dir: Path,
        *,
        reader: KubernetesReader | None = None,
    ) -> None:
        self.namespace = namespace
        self.output_dir = output_dir
        self.reader = reader if reader is not None else KubernetesReader()

    async def collect(self, label_selector: str = "") -> None:
        """Collect container logs and pod descriptions for all matching pods.

        Writes to :attr:`output_dir`:

        - ``{container}-{pod}.log`` — current container logs (tail 10 000 lines)
        - ``{container}-{pod}-previous.log`` — previous container logs (if any)
        - ``{pod}-describe.txt`` — ``kubectl describe pod``
        - ``pods-wide.txt`` — ``kubectl get pods -o wide``

        Args:
            label_selector: Narrows which pods are collected. Empty collects
                every pod in the namespace.
        """
        if not self._ensure_output_dir():
            return

        pods = await self._list_pods(label_selector)

        tasks: list[asyncio.Task[None]] = []
        loop = asyncio.get_running_loop()

        for pod in pods:
            tasks.append(
                loop.create_task(
                    _run_to_file(
                        kubectl_argv(
                            "describe",
                            "pod",
                            pod.name,
                            "-n",
                            self.namespace,
                            kube_context=self.reader.kube_context,
                        ),
                        self.output_dir / f"{pod.name}-describe.txt",
                    )
                )
            )
            for container, restarts in (pod.containers or {}).items():
                prefix = f"{container.replace('/', '-')}-{pod.name.replace('/', '-')}"
                tasks.append(
                    loop.create_task(
                        self._write_log(
                            pod.name,
                            container,
                            self.output_dir / f"{prefix}.log",
                            previous=False,
                        )
                    )
                )
                if restarts > 0:
                    tasks.append(
                        loop.create_task(
                            self._write_log(
                                pod.name,
                                container,
                                self.output_dir / f"{prefix}-previous.log",
                                previous=True,
                            )
                        )
                    )

        # Wide pod listing
        tasks.append(
            loop.create_task(
                _run_to_file(
                    kubectl_argv(
                        "get",
                        "pods",
                        "-n",
                        self.namespace,
                        "-o",
                        "wide",
                        kube_context=self.reader.kube_context,
                    ),
                    self.output_dir / "pods-wide.txt",
                )
            )
        )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Log collection task failed", exc_info=result)

    async def collect_events(self) -> None:
        """Write namespace events sorted by timestamp to ``events.txt``."""
        if not self._ensure_output_dir():
            return

        await _run_to_file(
            kubectl_argv(
                "get",
                "events",
                "-n",
                self.namespace,
                "--sort-by=.lastTimestamp",
                kube_context=self.reader.kube_context,
            ),
            self.output_dir / "events.txt",
        )

    def _ensure_output_dir(self) -> bool:
        """Create :attr:`output_dir`, reporting rather than raising on failure."""
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as exc:
            logger.warning(
                "Failed to create output dir %s error_type=%s",
                self.output_dir,
                type(exc).__name__,
                exc_info=True,
            )
            return False

    async def _list_pods(self, label_selector: str) -> list[PodState]:
        """List the pods to collect from, or none when the cluster is unreadable.

        The reader raises on an unreadable cluster, which is what every *graded*
        read wants. Here it is caught: an unreadable listing means there are no
        per-pod artefacts to write, and the namespace-wide ones — the wide listing
        and the events — are still worth having, and are often the very thing that
        explains why the listing failed.
        """
        try:
            return list(await self.reader.pods(self.namespace, label_selector))
        except Exception as exc:
            logger.warning(
                "Could not list pods in %s for log collection error_type=%s — "
                "collecting the namespace-wide artefacts only",
                self.namespace,
                type(exc).__name__,
                exc_info=True,
            )
            return []

    async def _write_log(
        self, pod: str, container: str, output_path: Path, *, previous: bool
    ) -> None:
        """Write one container's output to a file, reporting rather than raising."""
        try:
            text = await self.reader.container_log(
                self.namespace,
                pod,
                container,
                previous=previous,
                tail_lines=_TAIL_LINES,
            )
            # Explicit UTF-8: `write_text` otherwise uses the locale encoding,
            # which is cp1252 on Windows and cannot represent most of what a
            # container logs. `errors="replace"` because this is evidence — one
            # undecodable character must not cost the whole file.
            output_path.write_text(text, encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning(
                "Failed to collect %slogs for %s/%s/%s error_type=%s",
                "previous " if previous else "",
                self.namespace,
                pod,
                container,
                type(exc).__name__,
                exc_info=True,
            )
