"""Canonical Temporal task-queue naming (FND-195).

The task-queue name has to be agreed on by two code paths that never talk to
each other:

* the **worker**, which polls the queue — :func:`application_sdk.main._derive_task_queue`;
* the **served manifest**, whose resolved ``task_queue`` value the Automation
  Engine writes into the DAG and therefore submits work to —
  :mod:`application_sdk.handler.service`.

Before this module each side derived the name independently, from different
sources, with different unset-case semantics. When they disagreed nothing failed
loudly: AE submitted to one queue, the worker polled another, and the workflow
sat unclaimed until its 24h heartbeat backstop (CONNECT-183; the same gap
stripped failure attribution in HYP-1954).

There is now one derivation, :func:`derive_task_queue`, and the manifest side
does not even run it: :func:`resolve_manifest_tokens` *stamps* the queue the
handler was configured with — the same value ``create_worker`` receives. Two
paths deriving the same answer is a convention that holds until someone's inputs
differ; one path copying the other's answer is structural.

Two properties are load-bearing and easy to lose:

**The name is a unit, not an assembly of tokens.** The manifest carries
``atlan-<app>-{deployment_name}``, so it is tempting to just fill the token in
place. That is what diverged: with ``ATLAN_DEPLOYMENT_NAME`` unset the worker
drops the prefix entirely and polls a bare ``<app>``, while token-filling
produces ``atlan-<app>-local``; and a baked ``<app>`` that no longer matches the
deployment's ``ATLAN_APPLICATION_NAME`` is never corrected at all.
:func:`resolve_manifest_tokens` therefore matches the whole template and
replaces it outright.

**The unset case must stay loud.** ``constants.APPLICATION_NAME`` and
``constants.DEPLOYMENT_NAME`` manufacture ``"default"`` / ``"local"`` when the
env is unset. That is right for storage identity — a path prefix needs *some*
segment — and wrong for a queue name, because ``atlan-default-prod`` looks
entirely legitimate and reproduces the original hang. This module reads
``os.environ`` directly and reports "no name" as ``None`` so callers can fail or
leave the literal token visible; a literal ``{app_name}`` in a served manifest is
greppable and diagnosable in one step.

**Why a runtime fill exists at all, given the toolkit bakes these values.** This
looks like duplicated responsibility and is worth not re-litigating. The original
runtime substitution was proposed as #2270 and never merged; #2271 superseded it
by moving the fix into the toolkit (``App.pkl`` bakes ``app_name`` from the
contract ``name``), and #2478 extended that bake to the legacy ``NativeApp.pkl``
template. Those bakes are correct and this module does not second-guess them —
:func:`resolve_manifest_tokens` treats a baked name as the expected case.

What generation-time baking cannot reach, and what this module is for:

* **Adoption lag.** #2478's own rollout note is explicit that it does not touch
  already-committed ``manifest.json`` files — apps must bump the toolkit and
  regenerate. Until then their manifests ship a literal token, and #2271 removed
  the only fallback. #2271's own text anticipated this ("until they migrate, the
  runtime fill remains their path"); that fill never actually shipped, because
  #2270 was never merged.
* **Non-toolkit write paths.** Heracles, native-migration-app, and the
  install-time ``manifest_upgrade`` / ``schedule_reconciler`` in
  ``atlan-local-marketplace-app`` (CONNECT-191) *mutate or re-write* an AE DAG
  outside the toolkit's generation step, so the bake's guarantee never applied
  to them. Each hand-patched this gap independently; one of them shipped a
  double prefix (DISTR-834) by pre-prefixing an already-prefixed value.

So this is a reconciliation point for writers the toolkit does not own, not a
second mechanism competing with it. Retire it when those writers are retired.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Prefix the platform expects on a fully-qualified app queue. Callers pass the
#: *bare* app name to :func:`derive_task_queue` and let it add the prefix —
#: prefixing an already-prefixed value is how DISTR-834 shipped
#: ``atlan-atlan-dbt-production``, a queue no worker polls.
QUEUE_PREFIX = "atlan-"

APP_NAME_TOKEN = "{app_name}"
DEPLOYMENT_NAME_TOKEN = "{deployment_name}"

_APP_NAME_TOKEN_BYTES = APP_NAME_TOKEN.encode()
_DEPLOYMENT_NAME_TOKEN_BYTES = DEPLOYMENT_NAME_TOKEN.encode()


def derive_task_queue(app_name: str | None, deployment_name: str | None) -> str | None:
    """Derive the Temporal task-queue name for an app deployment.

    The single source of truth for the rule. Mirrors v2
    ``TemporalWorkflowClient.get_worker_task_queue()``:

    * app name **and** deployment name → ``atlan-{app}-{deployment}``
    * app name only → ``{app}`` (bare, unprefixed)
    * no app name → ``None``

    Args:
        app_name: Bare app name, **without** the ``atlan-`` prefix (the
            convention both the contract toolkit's bake and
            ``ATLAN_APPLICATION_NAME`` use). Blank/whitespace counts as unset.
        deployment_name: Deployment name. Blank/whitespace counts as unset.

    Returns:
        The queue name, or ``None`` when the app name is unavailable. ``None``
        is deliberate: there is no safe queue name to invent, and callers differ
        in what they should do about it (the worker falls back to a class-name
        queue for local dev; the manifest leaves the token visible and logs).
    """
    app = (app_name or "").strip()
    deployment = (deployment_name or "").strip()
    if app and deployment:
        return f"{QUEUE_PREFIX}{app}-{deployment}"
    if app:
        return app
    return None


def application_name_from_env() -> str:
    """``ATLAN_APPLICATION_NAME``, empty string when unset.

    Deliberately not ``constants.APPLICATION_NAME``, which substitutes the
    literal ``"default"`` — see the module docstring on why a manufactured app
    name is worse than none for queue naming.
    """
    return os.environ.get("ATLAN_APPLICATION_NAME", "").strip()


def deployment_name_from_env() -> str:
    """``ATLAN_DEPLOYMENT_NAME``, empty string when unset.

    Deliberately not ``constants.DEPLOYMENT_NAME``, which substitutes
    ``"local"``: the worker treats unset as "drop the prefix", so a manufactured
    ``"local"`` here would rebuild the very divergence this module exists to
    remove.
    """
    return os.environ.get("ATLAN_DEPLOYMENT_NAME", "").strip()


def task_queue_from_env() -> str | None:
    """:func:`derive_task_queue` applied to the deployment's own environment.

    Read live from ``os.environ`` rather than an import-time snapshot so the
    worker and the manifest-serving path cannot disagree just because one of
    them imported earlier than the env was set.
    """
    return derive_task_queue(application_name_from_env(), deployment_name_from_env())


@dataclass(frozen=True)
class ManifestTokenResolution:
    """Outcome of :func:`resolve_manifest_tokens`."""

    raw: bytes
    """The manifest bytes with every resolvable token substituted."""

    task_queue: str | None
    """The queue this app's own DAG nodes were stamped with — the one its worker
    polls. ``None`` when no queue name was determinable, in which case the queue
    template was left untouched rather than filled with a manufactured value."""

    app_name: str | None
    """The value used to fill ``{app_name}``, or ``None`` when no app name was
    available anywhere."""

    had_app_name_token: bool
    """``True`` when the manifest shipped a literal ``{app_name}``, i.e. the
    contract toolkit's bake did not reach it. Actionable regardless of whether
    :attr:`app_name` then filled it: it means the app's committed manifest is
    stale and the next writer of that DAG gets no guarantee at all."""

    @property
    def unresolved_app_name(self) -> bool:
        """A literal ``{app_name}`` survives in :attr:`raw`.

        The served manifest is not usable as-is; the surviving token is the
        diagnostic. Callers should log at ERROR.
        """
        return self.had_app_name_token and self.app_name is None


def resolve_manifest_tokens(
    raw: bytes,
    *,
    task_queue: str | None = None,
    app_name: str | None = None,
    deployment_fallback: str | None = None,
) -> ManifestTokenResolution:
    """Resolve ``{app_name}`` / ``{deployment_name}`` tokens in a served manifest.

    Ordering matters, and the queue rewrite must come first:

    1. **This app's queue template, matched whole and stamped with the queue the
       worker actually polls.** Both the un-baked
       ``atlan-{app_name}-{deployment_name}`` (a contract-toolkit miss, or a
       hand-authored manifest) and the baked ``atlan-<name>-{deployment_name}``
       are replaced outright. Filling the two tokens in place instead is what
       diverged: with no deployment name the worker drops the prefix and polls a
       bare ``<app>``, and a baked name that no longer matches the deployment's
       ``ATLAN_APPLICATION_NAME`` is never corrected at all.
    2. **Residual** ``{app_name}``, which is per-node log identity rather than a
       queue (``inputs.args.app_name``; a literal token reaching observability
       here is HYP-1954).
    3. **Residual** ``{deployment_name}``, e.g. on DAG nodes that dispatch to
       *another* app's queue (``atlan-publish-{deployment_name}`` and friends).
       Those are legitimately not this app's queue, so they are token-filled and
       otherwise left alone — neither normalised nor warned about.

    Args:
        raw: Raw manifest bytes. Not parsed — the contract tooling already
            validated the JSON at build time, and byte substitution keeps the
            serve path free of a parse/reserialize round-trip.
        task_queue: The queue the app's worker polls, when the caller knows it
            (the handler does: it is handed the same value ``create_worker`` gets).
            Preferred over re-deriving from env because it also carries an
            explicit ``ATLAN_TASK_QUEUE`` / ``--task-queue`` override, which no
            amount of re-derivation can reproduce. Falls back to
            :func:`task_queue_from_env`.
        app_name: The app's registered name — what the toolkit *would* have baked.
            Used both as a template candidate in step 1 and to fill step 2's
            token. Preferred over ``ATLAN_APPLICATION_NAME`` there because it is
            the value the Workflow Center's log filter matches (HYP-1678).
        deployment_fallback: Value for step 3 when ``ATLAN_DEPLOYMENT_NAME`` is
            unset. Defaults to ``"default"``, preserving the pre-FND-195
            behaviour of that token. Never used for step 1: manufacturing a
            deployment segment there is the divergence itself.

    Returns:
        A :class:`ManifestTokenResolution`.
    """
    env_app_name = application_name_from_env()
    deployment_name = deployment_name_from_env()
    registered_app_name = (app_name or "").strip()

    resolved_queue = task_queue or task_queue_from_env()
    resolved_app_name = registered_app_name or env_app_name or None
    had_app_name_token = _APP_NAME_TOKEN_BYTES in raw

    if resolved_queue is not None:
        encoded_queue = resolved_queue.encode()
        # Ordered so the un-baked token form is tried first and duplicates (the
        # common case where the registered name *is* ATLAN_APPLICATION_NAME)
        # collapse to one replace.
        candidates = [APP_NAME_TOKEN, registered_app_name, env_app_name]
        for candidate in dict.fromkeys(c for c in candidates if c):
            raw = raw.replace(
                f"{QUEUE_PREFIX}{candidate}-{DEPLOYMENT_NAME_TOKEN}".encode(),
                encoded_queue,
            )

    if resolved_app_name:
        raw = raw.replace(_APP_NAME_TOKEN_BYTES, resolved_app_name.encode())

    residual_deployment = deployment_name or (deployment_fallback or "default")
    raw = raw.replace(_DEPLOYMENT_NAME_TOKEN_BYTES, residual_deployment.encode())

    return ManifestTokenResolution(
        raw=raw,
        task_queue=resolved_queue,
        app_name=resolved_app_name,
        had_app_name_token=had_app_name_token,
    )
