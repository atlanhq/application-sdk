"""Starting a run through the Automation Engine (FND-243).

An extraction, not new work: this is ``BaseE2ETest._bootstrap_workflow`` plus
the submit half of ``run_full_dag``, with the connector's class attributes taken
as a :class:`~application_sdk.testing.harness.starters.AEWorkflowSpec` instead of
read off ``self``. Everything it calls is already a method on
:class:`~application_sdk.testing.harness.automation_engine.AEClient` — the four
AE writes moved there in child F — so what lands here is the *sequence*, which
was the last part of the AE path still trapped inside a base class.

**The sequence is the subject.** Four writes in a fixed order, each of which is
useless without the ones around it:

1. ``create_workflow`` — AE does not auto-create on submit; a fresh slug answers
   HTTP 404 with "Create the workflow first". Idempotent on the name.
2. ``wait_for_slug`` — AE has a brief indexing window before a fresh slug is
   queryable by ``/versions``. This *replaced* an unconditional ``time.sleep(3)``
   in child D (FND-240), which was wrong in both directions: three seconds
   charged to every run that did not need them, and no help at all on the run
   where indexing took four. The read is advisory rather than a gate —
   ``create_version`` retries on 404 for exactly this reason, and that retry is
   what makes the sequence safe either way.
3. ``create_version`` + ``publish_version`` — a workflow needs a *published*
   version before a submit against it is accepted.
4. ``submit_workflow`` — the one non-idempotent write in the harness.

**No error translation, deliberately.** Every step already raises a typed leaf
from :mod:`application_sdk.testing.harness.automation_engine`:
``AtlanApiHttpError`` for a rejected write, ``AtlanAEWorkflowAlreadyActiveError``
for the conflict Heracles masks as a 500, ``AppNotReadyError`` for a tenant pod
that never started serving. Wrapping those in a starter-specific leaf would
replace a category an operator already knows how to read with one that says only
"the starter failed", and would give the AE path a second classification of the
same responses — the divergence FND-224 exists to remove. The queue starter
converts ``temporalio``'s errors because ``temporalio`` has no leaves of its own;
this one has nothing to convert.

**Nothing is retried around the sequence.** A caller that re-ran the whole
function after a failed submit would re-enter that write's own inner retry, past
the ``retry_after`` honouring and the credential-name rotation that make each
re-POST safe — and the submit is the write where a duplicate is a *phantom run*
AE marks Skipped and returns under a fresh id, which the harness then polls
while the real run finishes elsewhere. The budget belongs inside the submit, and
:class:`~application_sdk.testing.harness.starters.SubmitRetry` is how a caller
widens it.

**The seed DAG is a value, not a build.** Resolving a manifest's mustache
tokens, picking the extract task queue, falling back to a legacy graph — those
stay with the connector suite. They are policy, and they are the reason
``BaseE2ETest`` exists at all. Taking the graph as a value is also what lets a
runtime scenario supply one that never came from a ``manifest.json``.
"""

from __future__ import annotations

import os

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.automation_engine.client import AEClient
from application_sdk.testing.harness.identity import Minter
from application_sdk.testing.harness.starters._specs import (
    AERunHandle,
    AEWorkflowSpec,
    SeededWorkflow,
)

logger = get_logger(__name__)

__all__ = ["publish_seed_version", "start_via_automation_engine"]


async def start_via_automation_engine(
    spec: AEWorkflowSpec, *, client: AEClient, minter: Minter | None = None
) -> AERunHandle:
    """Create, seed, publish and submit a workflow through the Automation Engine.

    Deliberately shares no signature with
    :func:`~application_sdk.testing.harness.starters.start_on_task_queue` or
    :func:`~application_sdk.testing.harness.starters.start_via_app_handler`:
    different inputs, different handles, nothing in the middle to widen.

    Args:
        spec: What to create, what to seed it with, and what to submit.
        client: An open AE client, on *this* event loop. Not closed here — the
            transport's lifetime stays with whoever opened it, the same rule the
            queue starter follows, and for the same reason: a caller that goes
            on to poll ``native-status`` through the same client would find it
            shut.
        minter: Supplies the seed version number when ``spec.version`` is
            ``None``. Injected for the reason
            :mod:`application_sdk.testing.harness.identity` exists: a number
            built from an unseeded clock is a number no test can predict, and
            this one is what later distinguishes the harness' own seed from the
            manifest the tenant published over it. ``None`` builds one over the
            real clock.

    Returns:
        The run handle, carrying AE's slug and the run id the submit reported.

    Raises:
        AtlanApiHttpError: If any of the four writes was rejected.
        AtlanApiResponseInvariantError: If a write succeeded but its response
            named no slug, version or run id.
        AtlanAEWorkflowAlreadyActiveError: If a run for this workflow is already
            active. Terminal, not retryable — a retry spawns a duplicate run AE
            marks ``Skipped``.
        AppNotReadyError: If the tenant app pod never accepted the submit across
            the whole cold-start budget.

    Example:
        >>> handle = await start_via_automation_engine(  # doctest: +SKIP
        ...     AEWorkflowSpec(
        ...         name=f"{connector}-{run_id}",
        ...         seed_dag=seed_dag,
        ...         payload=payload,
        ...         submit_retry=SubmitRetry.for_cold_start(
        ...             timeout_seconds=600, poll_interval_seconds=10
        ...         ),
        ...     ),
        ...     client=ae_client,
        ... )
    """
    seeded = await publish_seed_version(spec, client=client, minter=minter)
    slug, seed_version = seeded.slug, seeded.seed_version

    logger.info("submitting AE run against slug %s", slug)
    # ``slug=`` is what lets an ambiguous submit timeout be resolved by reading
    # AE's own run list rather than failing the leg: AE writes the run record
    # before it answers, so a response that never arrived is still recoverable.
    retry = spec.submit_retry
    if retry is None:
        run_id = await client.submit_workflow(dict(spec.payload), slug=slug)
    else:
        run_id = await client.submit_workflow(
            dict(spec.payload),
            slug=slug,
            retries=retry.retries,
            retry_sleep_seconds=retry.sleep_seconds,
        )
    logger.info("AE accepted the submit: slug=%s run_id=%s", slug, run_id)
    return AERunHandle(workflow_slug=slug, run_id=run_id, seed_version=seed_version)


async def publish_seed_version(
    spec: AEWorkflowSpec, *, client: AEClient, minter: Minter | None = None
) -> SeededWorkflow:
    """Return a slug with a published version, creating and seeding one if needed.

    The first three of :func:`start_via_automation_engine`'s four writes, public
    because the fourth cannot always be handed a payload built in advance. AE
    mints the slug on the create, and a submit body that has to *carry* that
    slug — ``metadata.ae_workflow_slug`` in
    :func:`application_sdk.testing.e2e.payload.build_ae_payload` — therefore
    cannot exist until this call has answered. ``BaseE2ETest`` is that caller:
    it publishes here, builds its payload around the slug, and submits through
    :meth:`~application_sdk.testing.harness.automation_engine.AEClient.submit_workflow`.

    Splitting the sequence in two does not weaken the rule the module docstring
    states — nothing may re-enter the submit's own retry. The submit is still one
    call, still the only non-idempotent write, and still nothing wraps a second
    loop around it. What is split is only *where the payload comes from*.

    A pre-existing ``spec.slug`` returns immediately and *nothing is seeded over
    it*. That is the point of the field rather than an optimisation: the workflow
    belongs to whoever set it up, and publishing a version over it would replace
    a DAG this run does not own — on a tenant where the next run to submit
    against that slug is not necessarily this one.

    Args:
        spec: What to create and what to seed it with. :attr:`AEWorkflowSpec.payload`
            and :attr:`AEWorkflowSpec.submit_retry` are not read here — they
            belong to the submit.
        client: An open AE client, on *this* event loop. Not closed here.
        minter: Supplies the seed version number when ``spec.version`` is
            ``None``. ``None`` builds one over the real clock.

    Returns:
        The slug, and the version this call published under it.

    Raises:
        AtlanApiHttpError: If any of the three writes was rejected.
        AtlanApiResponseInvariantError: If a write succeeded but its response
            named no slug or version.
    """
    if spec.slug:
        logger.info("using the pre-existing AE workflow slug %s", spec.slug)
        return SeededWorkflow(slug=spec.slug, seed_version=None)

    slug = await client.create_workflow(name=spec.name, description=spec.description)
    logger.info("created (or reused) AE workflow name=%s slug=%s", spec.name, slug)

    # Advisory, not a gate — see the module docstring. ``create_version``'s own
    # 404 retry is what makes the sequence safe whether or not this settles.
    await client.wait_for_slug(slug)

    version = spec.version if spec.version is not None else _minted_version(minter)
    published = await client.create_version(
        slug, {"version": version, "dag": dict(spec.seed_dag)}
    )
    # AE assigns the version, and the number it assigns is not required to be
    # the one that was asked for. Publishing the requested number instead of the
    # assigned one would 404 on a mismatch, and — worse — would leave the run
    # comparing its seed against a version it never published.
    logger.info("created seed version %d under slug %s", published, slug)
    await client.publish_version(slug, published)
    return SeededWorkflow(slug=slug, seed_version=published)


def _minted_version(minter: Minter | None) -> int:
    """Mint a seed version for a spec that did not carry one.

    Uses the factory even though only the clock half matters, so there is one
    construction path for a real-clock minter rather than two.
    """
    resolved = Minter.from_environment(os.environ) if minter is None else minter
    return resolved.seed_version()
