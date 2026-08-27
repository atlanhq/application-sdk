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
replaces it outright — and does so field-aware, rewriting only values the
manifest itself labels ``task_queue`` rather than substituting bytes anywhere
the template text happens to appear.

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

**The resolved value this module stamps is consumed outside this repo** — see
``docs/standards/cross-repo-contracts.md``. Renaming the ``task_queue`` key,
relocating it, or reverting to token substitution reds another repo's suite
with no signal here, so read that entry before changing either.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Prefix the platform expects on a fully-qualified app queue. Callers pass the
#: *bare* app name to :func:`derive_task_queue` and let it add the prefix —
#: prefixing an already-prefixed value is how DISTR-834 shipped
#: ``atlan-atlan-dbt-production``, a queue no worker polls.
QUEUE_PREFIX = "atlan-"

APP_NAME_TOKEN = "{app_name}"
DEPLOYMENT_NAME_TOKEN = "{deployment_name}"

_APP_NAME_TOKEN_BYTES = APP_NAME_TOKEN.encode()

#: The manifest field the queue rewrite owns. DAG nodes carry their dispatch
#: queue under this key at any depth (``dag.<node>.task_queue``,
#: ``dag.<node>.inputs.task_queue``, top-level on single-node manifests); only
#: string values under this exact key are this app's queue and may be stamped.
#: Every other byte in the manifest — foreign-app queues, descriptions,
#: metadata — is out of scope no matter how closely it matches the template.
_TASK_QUEUE_KEY = "task_queue"


def _rewrite_task_queue_fields(
    node: Any,
    rewrite: Callable[[str], str | None],
    app_name_fill: str,
    deployment_fill: str,
) -> bool:
    """Rewrite a parsed manifest in place: stamp own queues, fill the rest.

    Walks every string value. A ``task_queue`` value is handed to ``rewrite``,
    which returns the stamped queue, or ``None`` when the value is not this
    app's template and must be left alone. Every *other* string — log-identity
    ``app_name``, descriptions, foreign-app queue text, metadata — gets the
    residual ``{app_name}`` / ``{deployment_name}`` tokens filled.

    Field-aware stamping is the whole point: a queue name is only ever rewritten
    where the manifest *says* it is a queue, so a foreign-app DAG node whose
    baked queue matches the template, or a description string containing the
    template text, is never re-pointed at this app's worker (the byte
    substitutor did both). Splitting the residual fill by field also means the
    deployment/app fills can no longer mutate an already-stamped queue — the
    stamped field never sees them.

    Returns ``True`` when at least one ``task_queue`` value was visited.
    """
    visited = False
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                if key == _TASK_QUEUE_KEY:
                    stamped = rewrite(value)
                    visited = True
                    if stamped is not None:
                        # Own queue, stamped verbatim — the residual fills must
                        # not touch it, even when it carries literal token text.
                        node[key] = stamped
                        continue
                    # Not this app's template (``{deployment_name}-queue``,
                    # ``atlan-publish-{deployment_name}``): a residual field in
                    # all but key, so fall through and token-fill it.
                node[key] = value.replace(APP_NAME_TOKEN, app_name_fill).replace(
                    DEPLOYMENT_NAME_TOKEN, deployment_fill
                )
            else:
                visited = (
                    _rewrite_task_queue_fields(
                        value, rewrite, app_name_fill, deployment_fill
                    )
                    or visited
                )
    elif isinstance(node, list):
        for item in node:
            visited = (
                _rewrite_task_queue_fields(
                    item, rewrite, app_name_fill, deployment_fill
                )
                or visited
            )
    return visited


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

    Three jobs, done in one field-aware walk over the parsed manifest:

    1. **This app's queue, stamped with the queue the worker actually polls.**
       Every string ``task_queue`` value whose text matches the whole template —
       the un-baked ``atlan-{app_name}-{deployment_name}`` (a contract-toolkit
       miss, or a hand-authored manifest) or the baked
       ``atlan-<name>-{deployment_name}`` — is replaced outright. Filling the two
       tokens in place instead is what diverged: with no deployment name the
       worker drops the prefix and polls a bare ``<app>``, and a baked name that
       no longer matches the deployment's ``ATLAN_APPLICATION_NAME`` is never
       corrected at all.
    2. **Residual** ``{app_name}``, which is per-node log identity rather than a
       queue (``inputs.args.app_name``; a literal token reaching observability
       here is HYP-1954). Filled in every string that is not a ``task_queue``.
    3. **Residual** ``{deployment_name}``, e.g. on DAG nodes that dispatch to
       *another* app's queue (``atlan-publish-{deployment_name}`` and friends).
       Those are legitimately not this app's queue, so they are token-filled and
       otherwise left alone — neither normalised nor warned about.

    Two properties are load-bearing, and both come from the stamp owning the
    ``task_queue`` field rather than substituting bytes across the manifest:

    * *Field-aware.* Only a value the manifest itself labels ``task_queue`` is
      this app's queue. Whole-manifest byte substitution could not tell that
      from a foreign-app DAG node whose baked queue matches the template, or a
      description string containing the template text — it re-pointed both at
      this app's worker.
    * *The stamped queue is never re-filled.* A configured queue that itself
      contains literal token text (e.g. ``custom-{deployment_name}-queue`` via
      ``ATLAN_TASK_QUEUE``) is stamped verbatim: the residual fills skip
      ``task_queue`` values, so they can no longer mutate the stamped queue into
      one no worker polls while :attr:`ManifestTokenResolution.task_queue`
      reports the original.

    Args:
        raw: Raw manifest bytes. Parsed as JSON so the stamp can find the
            ``task_queue`` fields. A manifest too malformed to parse is served
            back unstamped and logged at ERROR rather than byte-substituted:
            byte substitution cannot scope the residual fills away from a
            stamped queue that carries literal token text, so it re-imports the
            very defect this module removes. A manifest that does not parse is
            one the build-time validation never saw — already broken, and better
            failed loud than served with a silently wrong queue.
        task_queue: The queue the app's worker polls, when the caller knows it
            (the handler does: it is handed the same value ``create_worker`` gets).
            Preferred over re-deriving from env because it also carries an
            explicit ``ATLAN_TASK_QUEUE`` / ``--task-queue`` override, which no
            amount of re-derivation can reproduce. Falls back to
            :func:`task_queue_from_env`. Stamped verbatim, even when it contains
            literal token text.
        app_name: The app's registered name — what the toolkit *would* have baked.
            Used both as a template candidate in the stamp and to fill the
            residual ``{app_name}`` token. Preferred over
            ``ATLAN_APPLICATION_NAME`` there because it is the value the Workflow
            Center's log filter matches (HYP-1678).
        deployment_fallback: Value for the residual ``{deployment_name}`` fill
            when ``ATLAN_DEPLOYMENT_NAME`` is unset. Defaults to ``"default"``,
            preserving the pre-FND-195 behaviour of that token. Never used for
            the queue stamp: manufacturing a deployment segment there is the
            divergence itself.

    Returns:
        A :class:`ManifestTokenResolution`.
    """
    env_app_name = application_name_from_env()
    deployment_name = deployment_name_from_env()
    registered_app_name = (app_name or "").strip()

    resolved_queue = task_queue or task_queue_from_env()
    resolved_app_name = registered_app_name or env_app_name or None
    had_app_name_token = _APP_NAME_TOKEN_BYTES in raw

    app_name_fill = resolved_app_name or APP_NAME_TOKEN
    deployment_fill = deployment_name or (deployment_fallback or "default")

    try:
        manifest = json.loads(raw)
        parse_error = False
    except (ValueError, UnicodeDecodeError):
        manifest = None
        parse_error = True

    if isinstance(manifest, (dict, list)):
        # Field-aware path: stamp own-queue fields, fill the residual tokens in
        # every other string, in one walk — so the deployment/app fills never
        # touch an already-stamped queue, and the stamp never touches a
        # foreign-app queue or a description string.
        _rewrite_task_queue_fields(
            manifest,
            _queue_stamper(resolved_queue, registered_app_name, env_app_name),
            app_name_fill,
            deployment_fill,
        )
        # ensure_ascii preserves the byte-for-byte ASCII shape of the toolkit's
        # own json.dumps output, so the reserialize is a no-op save the stamp.
        raw = json.dumps(manifest, ensure_ascii=True).encode()
    else:
        # A manifest whose task_queue fields cannot be located is served back
        # unstamped and logged at ERROR. That is either a parse failure (the
        # bytes are not JSON at all) or valid JSON with a scalar root (``null``,
        # a number, a string) that carries no object to walk. The pre-FND-195
        # byte substitutor this replaces could not scope the residual fills away
        # from a stamped queue carrying literal token text, so it re-imported the
        # defect this module removes. Such a manifest is one the build-time
        # validation never saw — already broken, and better failed loud than
        # served with a silently wrong queue.
        if parse_error:
            reason = "does not parse as JSON"
        else:
            reason = "parses as JSON but has a scalar root (no object to walk)"
        logger.error(
            "Served manifest %s, so its task_queue cannot be stamped "
            "field-aware; serving it unstamped rather than applying byte "
            "substitution, which cannot scope the residual token fills away "
            "from a stamped queue that carries literal token text. This "
            "manifest is malformed on disk — regenerate or restore it.",
            reason,
        )

    return ManifestTokenResolution(
        raw=raw,
        task_queue=resolved_queue,
        app_name=resolved_app_name,
        had_app_name_token=had_app_name_token,
    )


def _queue_stamper(
    resolved_queue: str | None,
    registered_app_name: str,
    env_app_name: str,
) -> Callable[[str], str | None]:
    """Build the field-aware ``task_queue`` rewrite for a parsed manifest.

    Returns a function mapping a ``task_queue`` value to the stamped queue when
    the value matches this app's whole ``atlan-{candidate}-{deployment_name}``
    template, else ``None`` (leave the value alone). When no queue is
    determinable every value is left alone, so the un-baked template stays
    visible rather than being filled with a manufactured name.
    """
    if resolved_queue is None:
        return lambda value: None

    # Ordered so the un-baked token form is tried first and duplicates (the
    # common case where the registered name *is* ATLAN_APPLICATION_NAME)
    # collapse to one entry.
    candidates = [APP_NAME_TOKEN, registered_app_name, env_app_name]
    templates = {
        f"{QUEUE_PREFIX}{candidate}-{DEPLOYMENT_NAME_TOKEN}"
        for candidate in dict.fromkeys(c for c in candidates if c)
    }
    return lambda value: resolved_queue if value in templates else None
