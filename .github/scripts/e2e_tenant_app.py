#!/usr/bin/env python3
"""Install the app under test onto an e2e tenant, and verify what is installed.

FND-31. The full-DAG e2e suite runs against a live tenant but never deployed the
app under test to it, so two things went wrong: where the app was installed the
tests exercised whatever version was last hand-deployed there, and where it was
not installed every leg died on a DNS lookup for a namespace that did not exist.

This is why version identity matters more than it looks: at AE submit, Heracles
re-fetches the manifest from the **tenant-deployed pod** and *that* DAG is what
runs (``processAutomationEngineWorkflow``). The harness's local seed DAG
establishes the workflow record, not the graph. So the DAG contract a full-DAG
e2e exercises is whatever is installed on that tenant.

Two subcommands:

``install``
    Register a tenant-targeted GM version for the PR image, install it, wait for
    the deployment to reconcile, and read the installed version back. Converges
    by version: when the tenant already runs the target version this is a no-op,
    which is what makes it safe to run once per (run x cloud) and again on a
    re-run.

``verify``
    Read the installed version and compare it to what was expected. Cheap enough
    to run per leg immediately before pytest, which is the point — the
    installation is tenant-scoped, so a concurrent run against the same tenant
    can replace it between the install and the assertions. There is no
    cross-job lease in GitHub Actions that closes that window, so the drift is
    turned into a loud failure instead of a silent wrong-version pass.

Credentials
-----------
Read from the environment, never from argv: argv is visible in process listings
and in ``set -x`` output.

* ``E2E_OAUTH_CLIENT_ID`` / ``E2E_OAUTH_CLIENT_SECRET``, falling back to
  ``SDR_CLIENT_ID`` / ``SDR_CLIENT_SECRET`` — the OAuth client pair. **Publish
  authorises on this**, not on the API key. The fallback names are what
  the e2e tenant resolver already writes, so an e2e leg needs no re-interpolation
  of the secret through a GitHub expression just to rename it.
* ``ATLAN_API_KEY`` — used for the read-only info/deployment routes, which the
  API key's ``realm-admin`` service account can reach. Falls back to the OAuth
  token when unset.

The release-scan gate
---------------------
``atlan app deploy`` waits up to 10 minutes for GM's async Snyk scan and refuses
to install a ``scan_failed`` release. That is wrong for e2e: a base-image CVE
would red every leg of an unrelated PR, and release is gated separately. So the
default here is ``--scan-wait-seconds 0`` — install immediately, report whatever
the scan status happens to be.

Answered empirically (FND-31 spike (b)), and not in the shape expected: GM does
not refuse the install. The constraint is one layer down — **LM cannot see the
release yet**. LM resolves an install against its own tenant-catalog snapshot,
which excludes a release while it is ``scan_pending`` and only picks it up on the
next scheduled sync (~5 min). A run that read ``active`` straight from GM still
had its install miss, so waiting on GM's release status is not sufficient.

Hence ``--install-retry-seconds`` (default 600): the install itself is retried
while LM catches up. ``--scan-wait-seconds`` stays at 0 — waiting on the scan
would not have helped, and a base-image CVE must not red an unrelated PR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from e2e_tenant_api import (
    APP_EVENTS_PATH,
    APP_FAILURE_PATH,
    APP_INFO_PATH,
    DEPLOYMENT_PATH,
    INSTALL_PATH,
    PUBLISH_PATH,
    RELEASE_SCAN_PATH,
    Response,
    TenantApiError,
    TenantClient,
    mint_oauth_token,
    path_segment,
    validate_app_id,
    validate_tenant_base_url,
    validate_tenant_id,
)
from marketplace_publish_body import PublishBodyError, PublishRequest, build

#: Terminal deployment states from LM.
_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"

#: Release states GM reports while the Snyk scan is in flight / has failed.
_SCAN_PENDING = "scan_pending"
_SCAN_FAILED = "scan_failed"

_DEPLOY_POLL_SECONDS = 10
_SCAN_POLL_SECONDS = 10
#: Gap between install retries while LM's catalog snapshot catches up.
_INSTALL_RETRY_POLL_SECONDS = 20

#: The two waits this script can spend, as module constants rather than argparse
#: literals, because a caller's job `timeout-minutes` has to stay above their sum:
#: if the runner's timeout fires first, a slow LM sync reports as "job cancelled"
#: and the actionable error this script was about to print is never written. The
#: workflows' guards assert their timeouts against these, so raising one here
#: fails the guard rather than silently making a job timeout reachable.
DEFAULT_INSTALL_RETRY_SECONDS = 600
DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS = 600

#: Keys an install/info response may carry the installed version under. LM has
#: not committed to one name across versions, so check the plausible set rather
#: than hard-coding a guess that silently reads None and compares equal to None.
#: ``version_text`` first: that is the field LM actually populates. Its
#: ``/apps/{id}/info`` returns ``{app_id, catalog, installed}`` where ``installed``
#: is an ``InstalledApp``
#: (``atlan-local-marketplace-app``, ``tenant_apps_manager/models/service.py``)::
#:
#:     app_id, version_id, version_text, installed_at, last_modified_on,
#:     deployment_name
#:
#: The rest are kept as tolerated aliases rather than removed — LM has not
#: committed to this shape across versions, and reading the wrong key returns ""
#: which is indistinguishable from "not installed", so a silent miss here reads as
#: a successful no-op instead of a broken check. Confirmed against the source
#: rather than guessed at: the original guess-list matched none of these fields,
#: so the version read never worked.
_VERSION_KEYS = (
    "version_text",
    "version",
    "installed_version",
    "app_version",
    "current_version",
)

#: Nests to search for the installed-version payload. ``installed`` is LM's real
#: envelope; the others are tolerated aliases.
_VERSION_NESTS = ("installed", "install", "installation", "deployment", "data")

#: LM's failure-snapshot fields (``deployment_orchestrator/failure_snapshot.py``),
#: in the order they answer "why did this deployment not become ready", with a
#: per-section line budget and which end to keep when it bites.
#:
#: The order is the whole point. The previous implementation dumped the payload
#: as ``json.dumps(..., sort_keys=True)[:8000]``, which sorts ``pod_describe``
#: ahead of ``pod_events`` — so the cut landed before the events on every failure,
#: and the events are where the registry says `no matching manifest for
#: linux/arm64`. Three FND-31 runs were misdiagnosed behind that truncation.
#:
#: ``pod_logs`` last because a container that never pulled has none, which is the
#: failure this ordering is tuned for; tail rather than head everywhere except
#: where the interesting end is the first (``failure_reason`` is a sentence).
_FAILURE_SECTIONS: tuple[tuple[str, int, str], ...] = (
    ("failure_reason", 20, "head"),
    ("pod_events", 200, "tail"),
    ("helmrelease_conditions", 60, "tail"),
    ("pod_describe", 200, "tail"),
    ("pod_logs", 120, "tail"),
)

_EVENTS_MAX_LINES = 200
_OTHER_FIELDS_MAX_LINES = 80

#: Registry and kubelet phrasings for "this image has no variant for my
#: architecture", matched case-insensitively across everything printed above.
_PLATFORM_MISMATCH_MARKERS = (
    "no matching manifest",
    "no match for platform",
    "does not match the specified platform",
)

#: Kubelet phrasings that mean "this pod could not get its image". Deliberately
#: excludes ``successfully pulled``, which names an image that is FINE — reading
#: it as a failure would invert the conclusion drawn from these.
_PULL_FAILURE_MARKERS = (
    "failed to pull image",
    "back-off pulling image",
    "imagepullbackoff",
    "errimagepull",
)

#: An image reference in kubelet's quoted form, e.g. `pulling image "ghcr.io/x:y"`.
_QUOTED_IMAGE_RE = re.compile(r'"([^"\s]+/[^"\s]+)"')

#: A pinned image reference is the same reference with ``@sha256:…`` in place of
#: the tag: `repo:tag@sha256:…`. Kubelet can report a pull failure in that form.
_PINNED_IMAGE_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


class TenantAppError(RuntimeError):
    """The install or verification failed."""


class DeploymentFailed(TenantAppError):
    """LM reported ``deployment_status: FAILED``.

    Separate from the base error because LM's verdict is **namespace-scoped**:
    its health check reports "Pods failed in namespace <ns>: <pod>" for ANY
    unhealthy pod in the app's namespace, including ones left behind by earlier
    installs of other versions. A tenant carrying an orphaned broken pod
    therefore fails every subsequent install regardless of whether the version
    being installed is fine.

    Observed on the first green multi-arch install: our own pods pulled the image
    in 12.4s and were scaled to zero by KEDA as designed, while a pod from an
    earlier attempt sat in ImagePullBackOff on a DIFFERENT tag
    (``x1048 over 3h59m``) and took the verdict down with it.

    So the caller checks whose pod is actually failing before deciding, and the
    installed-version read-back becomes the authority. A timeout stays a plain
    TenantAppError: nothing about it is somebody else's fault.
    """


@dataclass(frozen=True)
class InstallOutcome:
    """What an install actually did, for the step log and $GITHUB_OUTPUT."""

    version: str
    version_id: str = ""
    release_id: str = ""
    deployment_id: str = ""
    installed_version: str = ""
    release_status: str = ""
    skipped: bool = False

    def as_outputs(self) -> dict[str, str]:
        return {
            "version": self.version,
            "version_id": self.version_id,
            "release_id": self.release_id,
            "deployment_id": self.deployment_id,
            "installed_version": self.installed_version,
            "release_status": self.release_status,
            "skipped": "true" if self.skipped else "false",
        }


def _env(*names: str) -> str:
    """Return the first non-empty value among ``names``.

    Several names per credential so a call site does not have to re-interpolate a
    secret through a GitHub expression just to rename it. The e2e tenant
    resolver writes the leg's OAuth pair as ``SDR_CLIENT_ID`` /
    ``SDR_CLIENT_SECRET``, so
    those are accepted directly; the ``E2E_OAUTH_*`` names stay as the script's own
    generic contract for callers outside the e2e legs. Every hop a secret takes is
    a hop it can be mishandled on, so the fewer the better.
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def resolve_app_id(explicit: str) -> str:
    """Return *explicit* if set, else read ``app_id`` from ``atlan.yaml`` in cwd.

    Mirrors what the ``atlan`` CLI does (``resolveAppIDFromYaml``), and exists so a
    workflow step does not have to scrape another script's stdout to pass an id
    this one can read itself.

    ``yaml`` is imported lazily: the module is otherwise stdlib-only so it can run
    before the SDK is installed, and this fallback is the only path that needs a
    parser. A missing PyYAML is reported as such rather than as an ImportError
    traceback.
    """
    if explicit.strip():
        return explicit.strip()

    path = Path("atlan.yaml")
    if not path.is_file():
        raise TenantAppError(
            "no --app-id given and no atlan.yaml in the working directory "
            f"({Path.cwd()}). Pass --app-id, or run from the app repo root."
        )
    try:
        import yaml  # noqa: PLC0415 — lazy: only this fallback path needs it
    except ModuleNotFoundError as exc:  # pragma: no cover - present on CI runners
        raise TenantAppError(
            "reading app_id from atlan.yaml needs PyYAML, which is not importable. "
            "Pass --app-id explicitly instead."
        ) from exc

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    app_id = parsed.get("app_id", "") if isinstance(parsed, dict) else ""
    if not str(app_id).strip():
        raise TenantAppError(
            "atlan.yaml has no app_id. The app has not been registered in the "
            "marketplace yet, so there is nothing to install or verify against."
        )
    return str(app_id).strip()


def _clients(base_url: str) -> tuple[TenantClient, TenantClient]:
    """Return ``(publish_client, read_client)``.

    Two clients because two credentials: publish authorises on the OAuth client,
    while the read routes are reachable with the API key. Falling the read client
    back to the OAuth token keeps the script usable when only the pair is wired.
    """
    token = mint_oauth_token(
        base_url,
        _env("E2E_OAUTH_CLIENT_ID", "SDR_CLIENT_ID"),
        _env("E2E_OAUTH_CLIENT_SECRET", "SDR_CLIENT_SECRET"),
    )
    publish_client = TenantClient(base_url=base_url, bearer=token)
    api_key = _env("ATLAN_API_KEY")
    read_client = (
        TenantClient(base_url=base_url, bearer=api_key) if api_key else publish_client
    )
    return publish_client, read_client


def _installed_version(client: TenantClient, app_id: str) -> str:
    """Return the version the tenant currently reports for ``app_id``.

    Empty means "not installed, or LM did not report a version" — the caller must
    treat those the same way: install. Distinguishing them would require LM to
    commit to a shape it has not.
    """
    response = client.get(APP_INFO_PATH.format(app_id=path_segment(app_id)))
    if not response.ok:
        # Not fatal: a 404 is the "never installed on this tenant" case, which
        # FND-31 requirement 2 says to install rather than fail on.
        print(
            f"::notice::app info returned HTTP {response.status} for {app_id} — "
            "treating as not installed"
        )
        return ""
    return _extract_version(response.data())


def _app_info(client: TenantClient, app_id: str) -> dict[str, object]:
    """Return the app's info payload, or ``{}`` when it cannot be read."""
    response = client.get(APP_INFO_PATH.format(app_id=path_segment(app_id)))
    return response.data() if response.ok else {}


def _registered_source_repo(info: dict[str, object], _depth: int = 0) -> str:
    """Return the repo GM has on file for this app, or "" if it has none.

    GM's version-create guard is exactly (``global-marketplace``,
    ``core/app/service.py``)::

        if repo:                          # sets, or UPDATES, app.source_repo
            ...
        elif app.source_repo:             # no repo sent, but app is CI/CD-managed
            raise "This app's versions are managed by CI/CD..."

    So an app that has a ``source_repo`` rejects any version-create that omits
    ``repo``. Echoing GM's own value back would take neither the "set" nor the
    "update" branch — but **LM does not expose it**, so this almost always returns
    "" in practice and the caller has to supply the repo instead.

    Kept anyway, for two reasons: it costs one dict walk on a payload already
    fetched, and if LM ever surfaces the field the mismatch guard in
    :func:`_resolve_repo_url` starts catching a wrong ``--repo-url`` instead of
    merely warning about it. Verified against LM's router: ``/apps/{id}/info``
    returns ``{app_id, catalog, installed}`` and neither sub-object carries
    ``source_repo`` today.
    """
    if _depth >= _WALK_MAX_DEPTH:
        return ""
    for key in ("source_repo", "sourceRepo"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nest in ("app", "catalog", "data"):
        inner = info.get(nest)
        if isinstance(inner, dict):
            found = _registered_source_repo(inner, _depth + 1)
            if found:
                return found
    return ""


def _normalize_repo_url(value: str) -> str:
    """Canonicalize a repo URL for *comparison only* — never for sending.

    GitHub repo URLs are case-insensitive and may carry a trailing ``/`` or
    ``.git``. Two spellings of the same repo must not trip the mismatch guard
    (the GM-registered value is always what gets sent, so normalization here
    only decides whether to refuse, never what to publish).
    """
    normalized = value.strip().lower().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized.rstrip("/")


def _resolve_repo_url(registered: str, supplied: str) -> str:
    """Decide which ``repo`` to send, refusing to rewrite an app's provenance.

    GM does not merely validate ``repo`` — on a mismatch within the same GitHub
    org it **updates** ``app.source_repo`` to whatever was sent. So passing the
    repo of whichever CI happens to be running (rather than the app's own) would
    silently repoint the app's provenance and break the real CI/CD publish
    gating. That is a destructive side effect of a call that looks read-ish, so a
    disagreement is an error here rather than something to resolve by guessing.
    """
    registered, supplied = registered.strip(), supplied.strip()
    if (
        registered
        and supplied
        and _normalize_repo_url(registered) != _normalize_repo_url(supplied)
    ):
        raise TenantAppError(
            f"refusing to publish: GM has this app's source_repo as {registered!r} "
            f"but --repo-url is {supplied!r}. GM would UPDATE the app's "
            "source_repo to the supplied value (it only blocks cross-org "
            "changes), silently repointing the app's provenance and breaking its "
            "real CI/CD publish gating. Pass the app's own repo, or omit "
            "--repo-url and let the registered value be echoed back."
        )
    if not registered and not supplied:
        raise TenantAppError(
            "cannot publish without a repo: GM refuses a version-create that "
            "omits `repo` for any app that has a source_repo on file, which is "
            "every first-party app. LM's /apps/<id>/info does not expose the "
            "registered value, so it has to be supplied — pass --repo-url with "
            "the APP'S OWN repo (e.g. https://github.com/atlanhq/atlan-<x>-app).\n"
            "It must be the app's repo, NOT the repo whose CI is running: GM "
            "only blocks CROSS-ORG source_repo changes, so a same-org value is "
            "silently applied and would repoint the app's provenance."
        )
    # Send the registered value when present — byte-for-byte what GM has on
    # file — so the publish can neither trip the CI/CD guard nor rewrite
    # provenance. Normalization above only decided *whether* to refuse.
    return registered or supplied


def _repo_from_image(image: str) -> str:
    """Best-effort guess of the app's GitHub repo from its image reference.

    ``ghcr.io/atlanhq/atlan-openapi-app:tag`` -> ``https://github.com/atlanhq/atlan-openapi-app``.

    A cross-check only, never a source. The convention (GHCR image name == repo
    name) holds across the connector fleet but is a convention, not a guarantee,
    so a disagreement fails closed only for ``ghcr.io`` references — the
    confirmed e2e registry, where the convention does hold — and warns otherwise,
    so a legitimate exception off GHCR can still publish. It exists because the
    one destructive mistake available here is passing the wrong repo, and that
    mistake is usually visible in exactly this comparison.
    """
    ref = image.split("@", 1)[0]
    path = ref.rsplit(":", 1)[0] if ":" in ref.rsplit("/", 1)[-1] else ref
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or "." not in parts[0]:
        return ""
    return f"https://github.com/{parts[1]}/{parts[-1]}"


def _is_ghcr_image(image: str) -> bool:
    """True when the image reference is on GHCR, the confirmed e2e registry.

    Compared on the host with any explicit port stripped (``ghcr.io:443/...`` is
    still a GHCR reference), and case-insensitively — the fail-closed mismatch
    guard keys on this, so the one spelling variant that would otherwise slip
    into the warn-only path must not.
    """
    ref = image.split("@", 1)[0]
    authority = ref.split("/", 1)[0]
    return authority.rsplit(":", 1)[0].lower() == "ghcr.io"


#: How deep the recursive payload walks below may descend. Both LM walks start
#: at depth 0 and their nest lists are one level long, so real payloads never go
#: past depth 1; the bound exists so a pathological (deeply nested or, via
#: ``data``, self-referential) payload degrades to "" — which the callers already
#: treat as "field absent" — instead of crashing the step with a RecursionError
#: traceback. The nests walked are exactly the keys a JSON wrapper envelope uses
#: for self-similar nesting, so an unbounded walk is not theoretical.
_WALK_MAX_DEPTH = 3


def _extract_version(payload: dict[str, object], _depth: int = 0) -> str:
    """Pull the installed version out of an ``/apps/{id}/info`` payload.

    Searches the nests BEFORE the top-level keys. LM's envelope carries the
    installed state under ``installed``, while the sibling ``catalog`` block
    describes the app in general — so a top-level-first search risks reading a
    catalogue-level version and reporting it as what the tenant is running, which
    would make the version check pass against the wrong thing. Recursion is
    depth-bounded (see ``_WALK_MAX_DEPTH``).
    """
    if _depth >= _WALK_MAX_DEPTH:
        return ""
    for nest in _VERSION_NESTS:
        inner = payload.get(nest)
        if isinstance(inner, dict):
            found = _extract_version(inner, _depth + 1)
            if found:
                return found
    for key in _VERSION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


#: Cap on rendered response bodies in error messages. The token-mint path
#: withholds bodies entirely because they can echo request parameters; the
#: publish/install routes render a bounded prefix instead — enough of a live
#: tenant error page to diagnose a 4xx, not enough to dump a verbose framework
#: page (or anything auth-adjacent it carries) into the CI log unredacted.
_ERROR_BODY_CHARS = 2000


def _render_body(body: object) -> str:
    """Render a response body for an error message, length-bounded."""
    rendered = repr(body)
    if len(rendered) > _ERROR_BODY_CHARS:
        rendered = f"{rendered[:_ERROR_BODY_CHARS]}…(truncated)"
    return rendered


def _looks_like_cicd_managed(response: Response) -> bool:
    """True when a failed publish is GM's CI/CD-managed version guard.

    Matched on the rendered body: Heracles wraps GM's 409 inside its own 400, so
    the status alone does not identify it.
    """
    rendered = json.dumps(response.body).lower() if response.body else ""
    return "managed by ci/cd" in rendered or "cicd_managed" in rendered


def _looks_like_scan_gate_text(text: str) -> bool:
    """Scan-gate detection over a plain message string.

    LM reports its outcome in the body rather than the HTTP status, so the
    install path matches on the message while the publish path still matches on
    a whole response.
    """
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (_SCAN_PENDING, _SCAN_FAILED, "scan", "not active", "draft")
    )


def _looks_like_scan_gate(response: Response) -> bool:
    """True when a failed install looks like GM's release-scan gate refusing.

    Matched on the rendered body rather than a status code: GM does not document
    a distinct status for this, and a false positive only changes the wording of
    an error that is failing anyway.
    """
    rendered = json.dumps(response.body).lower() if response.body else ""
    return any(
        marker in rendered
        for marker in (_SCAN_PENDING, _SCAN_FAILED, "scan", "not active", "draft")
    )


def _release_status(client: TenantClient, app_id: str, release_id: str) -> str:
    """Return GM's release status, or "" when it cannot be read.

    Informational only — nothing gates on it. It is reported so the first live
    run answers whether an install of a ``scan_pending`` release is accepted.
    """
    if not release_id:
        return ""
    response = client.get(
        RELEASE_SCAN_PATH.format(
            app_id=path_segment(app_id), release_id=path_segment(release_id)
        )
    )
    if not response.ok:
        return ""
    status = response.data().get("status")
    return status.strip() if isinstance(status, str) else ""


def _wait_for_scan(
    client: TenantClient, app_id: str, release_id: str, budget: int
) -> str:
    """Poll the release scan until it leaves ``scan_pending`` or the budget ends.

    Only called when ``--scan-wait-seconds`` is non-zero, i.e. when an operator
    has deliberately opted back into waiting.
    """
    deadline = time.monotonic() + budget
    status = _release_status(client, app_id, release_id)
    while status == _SCAN_PENDING and time.monotonic() < deadline:
        time.sleep(_SCAN_POLL_SECONDS)
        status = _release_status(client, app_id, release_id)
    return status


def _publish(client: TenantClient, request: PublishRequest) -> tuple[str, str]:
    """Register the version. Returns ``(version_id, release_id)``."""
    try:
        body = build(request)
    except PublishBodyError as exc:
        raise TenantAppError(str(exc)) from exc

    response = client.post(PUBLISH_PATH, body=body)
    if not response.ok:
        # 401/403 here is the credential question: publish authorises on the
        # OAuth client, so name that explicitly rather than leaving an operator
        # to guess which of two credentials was rejected.
        hint = ""
        if response.status in (401, 403):
            hint = (
                " Publish authorises on the OAuth client pair "
                "(E2E_OAUTH_CLIENT_ID/SECRET), not on ATLAN_API_KEY — check that "
                f"pair. Token realm roles: {client.token_roles() or 'none visible'}."
            )
        elif _looks_like_cicd_managed(response):
            # GM rejects a version-create that omits `repo` when the app has a
            # source_repo on file. Reaching here means the echo-back above found
            # nothing, so say which read failed rather than restating GM's
            # message, which points at atlan.yaml and is misleading here.
            hint = (
                " GM refuses a version-create without `repo` for an app that has "
                "a source_repo on file. This normally self-resolves by echoing "
                "GM's registered source_repo back, so reaching this means "
                "/marketplace/apps/<id>/info did not expose it — check that read, "
                "or pass --repo-url with the APP'S OWN repo (not the repo whose "
                "CI is running: GM would repoint the app's provenance to it)."
            )
        raise TenantAppError(
            f"marketplace publish failed with HTTP {response.status}.{hint}\n"
            f"response={_render_body(response.body)}"
        )

    data = response.data()
    version_id = str(data.get("version_id") or "")
    release_id = str(data.get("release_id") or "")
    if not version_id:
        raise TenantAppError(
            f"publish returned no version_id (keys: {sorted(data)}); cannot install"
        )
    return version_id, release_id


@dataclass(frozen=True)
class _InstallReply:
    """LM's install response, whose HTTP status is not the whole story.

    ``POST /tenant/default/apps/{id}/install`` answers **HTTP 200 with an
    error-shaped envelope** for its two non-deploying outcomes
    (``atlan-local-marketplace-app``, ``marketplace_api/v1/router.py``)::

        {"status": "error",   "message": "App with ID '…' not found: …", "status_code": 404}
        {"status": "success", "message": "App already installed",        "status_code": 200}

    So ``response.ok`` alone would read a 404 as a success. The in-body
    ``status_code`` is authoritative and is what this parses.
    """

    http_status: int
    status: str
    status_code: int
    message: str
    deployment_id: str
    rendered_body: str

    @classmethod
    def parse(cls, response: Response) -> _InstallReply:
        data = response.data() if isinstance(response.body, dict) else {}
        raw_code = data.get("status_code")
        # `message` is LM's 200-envelope field; `detail` is what Heracles/FastAPI
        # put on a real HTTP error. Both have to be read, or a genuine 4xx loses
        # its text and the failure stops being self-explaining.
        message = str(data.get("message") or data.get("detail") or "").strip()
        return cls(
            http_status=response.status,
            status=str(data.get("status") or "").strip().lower(),
            # Fall back to the HTTP status when LM omits its own.
            status_code=raw_code if isinstance(raw_code, int) else response.status,
            message=message,
            deployment_id=str(data.get("deployment_id") or ""),
            rendered_body=_render_body(response.body),
        )

    @property
    def failed(self) -> bool:
        return self.status == "error" or self.status_code >= 400 or not self.http_ok

    @property
    def http_ok(self) -> bool:
        return 200 <= self.http_status < 300

    @property
    def not_found(self) -> bool:
        """The release is not resolvable in LM's tenant-catalog snapshot yet."""
        return self.status_code == 404 or "not found" in self.message.lower()

    @property
    def already_installed(self) -> bool:
        return not self.failed and "already installed" in self.message.lower()


def _install(
    client: TenantClient,
    app_id: str,
    version_id: str,
    scan_hint: str,
    *,
    retry_seconds: int,
) -> str:
    """Trigger the install, tolerating LM's catalog-snapshot lag.

    Returns the deployment id, or "" when LM reports the app is already
    installed (it does not start a deployment, so there is nothing to poll).

    The retry exists because a freshly published release is not immediately
    installable, which LM documents against this very route: a release created
    via ``POST /publish`` starts ``scan_pending`` and is excluded from GM's tenant
    catalog, the publish-time refresh runs while it is still ``scan_pending``, and
    it therefore does not enter LM's snapshot until the next scheduled sync
    (~5 min). LM refreshes once inline on a miss, but that refresh can itself race
    the flip to ``active``.

    Waiting on GM's release status is NOT sufficient: the first run to get this
    far read ``active`` from GM and the install still missed, because what install
    resolves against is LM's snapshot, not GM.
    """
    deadline = time.monotonic() + max(retry_seconds, 0)
    attempt = 0
    while True:
        attempt += 1
        reply = _InstallReply.parse(
            client.post(
                INSTALL_PATH.format(app_id=path_segment(app_id)),
                body={"version_id": version_id, "force_install": True},
            )
        )

        if reply.already_installed:
            print(f"LM reports the app already installed: {reply.message}")
            return ""

        if not reply.failed and reply.deployment_id:
            return reply.deployment_id

        if not reply.failed:
            raise TenantAppError(
                "install reported success but carried no deployment_id, so the "
                f"deployment cannot be polled. message={reply.message!r} "
                f"status={reply.status!r} status_code={reply.status_code}"
            )

        # Retryable: LM cannot see the release yet.
        if reply.not_found and time.monotonic() < deadline:
            remaining = int(deadline - time.monotonic())
            print(
                f"attempt {attempt}: LM cannot resolve the release yet "
                f"({reply.message or 'not found'}) — its catalog snapshot lags a "
                f"fresh publish by up to ~5 min; retrying for {remaining}s"
            )
            time.sleep(_INSTALL_RETRY_POLL_SECONDS)
            continue

        hint = ""
        if reply.not_found:
            hint = (
                " LM never saw the release. Its tenant-catalog snapshot excludes a "
                "release while it is scan_pending and only picks it up on the next "
                "scheduled sync (~5 min), so a fresh publish is not immediately "
                "installable. Raise --install-retry-seconds if the sync is slower "
                "than the current budget."
                + (f" GM release status was {scan_hint!r}." if scan_hint else "")
            )
        elif _looks_like_scan_gate_text(reply.message):
            hint = (
                " This looks like GM's release-scan gate. e2e deliberately does "
                "not wait for the scan (a base-image CVE must not red an unrelated "
                "PR); re-run with --scan-wait-seconds set if it is required."
            )
        raise TenantAppError(
            f"install failed after {attempt} attempt(s): "
            f"status={reply.status!r} status_code={reply.status_code} "
            f"message={reply.message!r} (HTTP {reply.http_status}).{hint}\n"
            f"response={reply.rendered_body}"
        )


def _poll_deployment(client: TenantClient, deployment_id: str, timeout: int) -> None:
    """Wait for the deployment to reach a terminal state.

    A timeout is a failure, not a pass-with-a-warning: an install that was
    accepted but never reconciled is exactly the silent-wrong-version failure
    this whole change exists to remove.
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        response = client.get(
            DEPLOYMENT_PATH.format(deployment_id=path_segment(deployment_id))
        )
        if response.ok:
            status = str(response.data().get("deployment_status") or "")
            if status != last:
                print(f"deployment {deployment_id}: {status or '<no status>'}")
                last = status
            if status == _SUCCEEDED:
                return
            if status == _FAILED:
                message = response.data().get("message")
                # A distinct type, because the caller treats this differently
                # from a timeout: LM's verdict is namespace-scoped and can be
                # about somebody else's pod (see DeploymentFailed).
                raise DeploymentFailed(
                    f"deployment {deployment_id} FAILED"
                    + (f": {message}" if message else "")
                )
        else:
            # Transient: keep polling rather than failing on one bad read.
            print(
                f"::warning::deployment status read returned HTTP {response.status}; retrying"
            )
        time.sleep(_DEPLOY_POLL_SECONDS)
    raise TenantAppError(
        f"deployment {deployment_id} did not reach a terminal state within "
        f"{timeout}s (last status: {last or 'none'}). Not treating this as "
        "installed — an accepted-but-unreconciled deploy is the silent "
        "wrong-version failure this check exists to catch."
    )


def _print_block(title: str, text: str, max_lines: int, keep: str = "tail") -> None:
    """Print one diagnostic section as real multi-line text.

    Two things this does that a ``json.dumps`` of the whole payload does not: the
    newlines are newlines (kubectl output rendered as one line of ``\\n`` escapes
    is not readable in a CI log), and an over-long section says how much it
    dropped and from which end, rather than stopping mid-word.
    """
    lines = text.rstrip().splitlines()
    if not lines:
        return
    note = ""
    if len(lines) > max_lines:
        note = f" (showing {keep} {max_lines} of {len(lines)} lines)"
        lines = lines[-max_lines:] if keep == "tail" else lines[:max_lines]
    print(f"--- {title}{note} ---")
    for line in lines:
        print(line)


def _hint_platform_mismatch(text: str) -> None:
    """Name the multi-arch cause when the diagnostics carry its signature.

    This is the single most expensive misdiagnosis in FND-31: the image was
    amd64-only, the tenant node was not, and the pull failure read as a pruned
    tag for three runs. The registry's own wording is unambiguous, so when it
    appears the cause is stated outright rather than left to be inferred again.
    """
    lowered = text.lower()
    if not any(marker in lowered for marker in _PLATFORM_MISMATCH_MARKERS):
        return
    print(
        "::error::the tenant could not pull the image because it has no variant "
        "for that node's architecture — the events above say so. The image must "
        "be multi-arch: build-app-image takes a `platforms` input, and the "
        "install path passes linux/amd64,linux/arm64 (amd64 is still needed, the "
        "per-leg worker runs on the runner). A single-arch image publishes, "
        "installs, and only fails here."
    )


def failing_images(diagnostics: str) -> list[str]:
    """Return the image references named by pull-failure lines, in order seen.

    Kubernetes phrases these as ``Failed to pull image "<ref>": …`` and
    ``Back-off pulling image "<ref>"``, both of which carry the ref in quotes. Only
    failure lines are read: a ``Successfully pulled image "<ref>"`` line names an
    image that is fine, and counting it would invert the conclusion.
    """
    found: list[str] = []
    for line in diagnostics.splitlines():
        lowered = line.lower()
        if not any(marker in lowered for marker in _PULL_FAILURE_MARKERS):
            continue
        for ref in _QUOTED_IMAGE_RE.findall(line):
            if ref not in found:
                found.append(ref)
    return found


def _image_repository(reference: str) -> str:
    """Return the part of an image reference before any ``@digest`` or ``:tag``.

    Two references in the same repository are the same image at different
    resolutions. ``reference.split("@")[0].rsplit(":", 1)[0]`` cannot express
    that: an UNtagged reference ends in a colon-free final segment, and rsplit
    would mistake that segment for a tag and cut it off — the safe direction of
    a misread here is treating too MUCH as the same image, never less, so the
    last colon only counts when a tag follows it.
    """
    reference = reference.strip().split("@", 1)[0]
    # The last colon is a tag separator only when it sits in the FINAL
    # slash-segment (the heuristic `_repo_from_image` already uses): in
    # ``ghcr.io:5000/org/repo`` the colon is the registry's port, and cutting
    # there reads the repository as bare ``ghcr.io`` — which can read OUR
    # failing image as foreign, the misread this module must never make.
    if ":" not in reference.rsplit("/", 1)[-1]:
        return reference
    return reference.rpartition(":")[0]


def _image_tag(reference: str) -> str:
    """Return the tag of an image reference, or "" when it carries none.

    Digest-free first: ``repo:tag@sha256:…`` and ``repo@sha256:…`` both lose the
    digest so only the ``repo[:tag]`` form is left. Then the last colon counts
    only when a tag follows it — on a colon-free reference ``rpartition`` hands
    back the whole string, which would read the repository AS the tag.
    """
    reference = _PINNED_IMAGE_RE.sub("", reference.strip())
    # Same final-segment rule as `_image_repository`: a colon left of the last
    # slash is a registry port (``ghcr.io:5000/org/repo``), not a tag, and
    # reading ``5000/org/repo`` as the tag breaks the same-repository compare.
    if ":" not in reference.rsplit("/", 1)[-1]:
        return ""
    head, sep, tag = reference.rpartition(":")
    return tag if sep and head else ""


def foreign_failure(diagnostics: str, image: str) -> list[str]:
    """Return failing images that are NOT *image*, or [] if ours is among them.

    An empty list means "do not second-guess LM": either nothing identifiable is
    failing, or our own image is one of the things failing. Only when every
    failing image is provably a different version does the caller get to treat
    the verdict as being about somebody else's pod.

    Proven-different, not string-different: kubelet can report a pull failure
    pinned (``…@sha256:…``, optionally with the tag in front) while ``--image``
    arrives as a tag, and exact-equality reads that failure of OUR image as
    foreign — the one misread this override must never make. So a failing
    reference counts as ours whenever it shares our repository and its tag is
    ours, a digest, or absent; only a different repository, or a resolvably
    different tag of ours, is foreign.
    """
    failing = failing_images(diagnostics)
    if not failing:
        return []
    ours = image.strip()
    ours_repo = _image_repository(ours)
    ours_tag = _image_tag(ours)
    for ref in failing:
        if _image_repository(ref) != ours_repo:
            continue  # a different repository: provably somebody else's pod
        ref_tag = _image_tag(ref)
        if not ref_tag or not ours_tag or ref_tag == ours_tag:
            return []  # same repository and tag, digest-pinned, or untagged
    return failing


def _dump_failure(client: TenantClient, app_id: str) -> str:
    """Print LM's failure diagnostics. Best-effort — never masks the real error.

    Reads two routes, because they fail in different circumstances and the
    important one is the second:

    ``/apps/{id}/failure``
        The snapshot LM captured at the moment the HelmRelease went
        ``Ready=False`` — ``failure_reason``, ``pod_events``, ``pod_describe``,
        ``pod_logs``, ``helmrelease_conditions``
        (``deployment_orchestrator/failure_snapshot.py``). 404s when no snapshot
        was captured, which includes every case where we timed out rather than
        the deployment being declared failed.

    ``/apps/{id}/events``
        Live ``kubectl get events`` for the app's namespace. Works with no
        snapshot at all, and is where an image-pull failure names itself.

    Sections are rendered in the order they answer "why did this not become
    ready", each with its own line budget. The previous version dumped the whole
    JSON payload truncated at 8000 characters: ``sort_keys`` put ``pod_describe``
    ahead of ``pod_events``, so the cut landed *before* the events every time —
    and the events were where `no matching manifest for linux/arm64` was waiting.
    """
    diagnostics: list[str] = []

    snapshot: dict[str, object] = {}
    try:
        response = client.get(APP_FAILURE_PATH.format(app_id=path_segment(app_id)))
    except TenantApiError as exc:
        print(f"::warning::could not fetch the failure snapshot: {exc}")
    else:
        if response.ok:
            body = response.body if isinstance(response.body, dict) else {}
            nested = body.get("snapshot")
            snapshot = dict(nested) if isinstance(nested, dict) else dict(body)
        else:
            # Expected on the timeout path: a deployment that never reached
            # FAILED has no snapshot to read.
            print(
                f"::warning::no failure snapshot for {app_id} "
                f"(HTTP {response.status}); falling back to live events"
            )

    for field, max_lines, keep in _FAILURE_SECTIONS:
        text = str(snapshot.pop(field, "") or "")
        diagnostics.append(text)
        _print_block(field, text, max_lines, keep)

    # Whatever is left, so a new snapshot field is visible the day it ships
    # instead of being silently dropped by the list above.
    leftovers = {k: v for k, v in snapshot.items() if v not in ("", None, {}, [])}
    if leftovers:
        _print_block(
            "other snapshot fields",
            json.dumps(leftovers, indent=2, sort_keys=True),
            _OTHER_FIELDS_MAX_LINES,
            "head",
        )

    # Live events. Fetched even when the snapshot had its own copy: the snapshot
    # is from the moment of failure and these are from now, and on the timeout
    # path they are the only events there are.
    try:
        events = client.get(APP_EVENTS_PATH.format(app_id=path_segment(app_id)))
    except TenantApiError as exc:
        print(f"::warning::could not fetch live events: {exc}")
    else:
        if events.ok:
            body = events.body if isinstance(events.body, dict) else {}
            text = str(body.get("events") or "")
            diagnostics.append(text)
            _print_block("live namespace events", text, _EVENTS_MAX_LINES)
        else:
            print(f"::warning::live events read returned HTTP {events.status}")

    collected = "\n".join(diagnostics)
    _hint_platform_mismatch(collected)
    # Returned so the caller can work out WHOSE pod failed without a second
    # round of API calls: everything needed is already in this text.
    return collected


def install(args: argparse.Namespace) -> InstallOutcome:
    """Register + install + wait, converging by version."""
    # app_id is a free-text workflow input that lands in request paths; the
    # base URL takes the OAuth secret. Validate both before any API call.
    #
    # Resolve BEFORE validating: an app_id read out of atlan.yaml must clear the
    # same UUID check as one passed on argv, or the fallback becomes a way around
    # the validator.
    app_id = validate_app_id(resolve_app_id(args.app_id))
    base_url = validate_tenant_base_url(args.base_url)
    publish_client, read_client = _clients(base_url)

    # One info read serves two purposes: the installed version (to converge on)
    # and the repo GM has on file (which the version-create needs echoed back).
    info = _app_info(read_client, app_id)
    if not info:
        print(f"::notice::app info unreadable for {app_id} — treating as not installed")
    current = _extract_version(info)
    if current and current == args.version:
        print(f"tenant already runs {app_id} at {args.version} — nothing to do")
        return InstallOutcome(
            version=args.version, installed_version=current, skipped=True
        )
    if current:
        print(f"tenant runs {current}; installing {args.version}")
    else:
        print(f"{app_id} is not installed on this tenant; installing {args.version}")

    # `repo` is mandatory in practice: GM rejects a version-create without it for
    # any app that has a source_repo on file. It prefers GM's own registered value
    # when readable (byte-for-byte, so no provenance rewrite), and otherwise takes
    # the caller's --repo-url, because LM's info does not expose it.
    repo_url = _resolve_repo_url(_registered_source_repo(info), args.repo_url)
    print(f"publishing with repo={repo_url}")

    # Cross-check against the repo the image implies. Passing the wrong repo is
    # the one destructive mistake available here — GM would repoint the app's
    # provenance — and it usually shows up as exactly this disagreement. Fail
    # closed when the image is a ghcr.io reference: GHCR is the confirmed e2e
    # registry, and there image-name == repo-name holds, so a disagreement is a
    # provenance-rewrite attempt, not a legitimate exception. Any other registry
    # only warns — the convention is not guaranteed to hold there, so a real
    # exception must still be able to publish.
    implied = _repo_from_image(args.image)
    if implied and _normalize_repo_url(implied) != _normalize_repo_url(repo_url):
        explanation = (
            f"repo {repo_url} does not match the repo implied by the image "
            f"({implied}). If {repo_url} is not this app's own repo, GM will "
            "repoint the app's source_repo to it and break its CI/CD publish "
            "gating."
        )
        if _is_ghcr_image(args.image):
            raise TenantAppError(
                f"refusing to publish: {explanation} The image is a ghcr.io "
                "reference, where image name == repo name holds across the "
                "fleet, so this disagreement is a wrong --repo-url, not a "
                "legitimate exception. Pass the app's own repo."
            )
        print(f"::warning::{explanation} Double-check before relying on this run.")

    request = PublishRequest(
        app_id=app_id,
        image=args.image,
        version=args.version,
        branch=args.branch,
        repo_url=repo_url,
        # The whole registration is scoped to this one tenant, so a per-PR build
        # can never become visible to a real one.
        allowed_tenants=(validate_tenant_id(args.tenant),),
        deploy_config=args.deploy_config,
        self_deployed_runtime=args.self_deployed_runtime,
        sdk_version=args.sdk_version,
        entrypoints=args.entrypoints,
        app_configs=args.app_configs,
        release_model=args.release_model,
        created_by=args.created_by,
    )
    version_id, release_id = _publish(publish_client, request)
    print(f"registered version_id={version_id} release_id={release_id}")

    if args.scan_wait_seconds > 0:
        status = _wait_for_scan(
            publish_client, app_id, release_id, args.scan_wait_seconds
        )
        print(f"release status after waiting: {status or '<unreadable>'}")
    else:
        status = _release_status(publish_client, app_id, release_id)
        print(
            f"release status at install time: {status or '<unreadable>'} "
            "(not waiting for the scan by design)"
        )

    deployment_id = _install(
        publish_client,
        app_id,
        version_id,
        status,
        retry_seconds=args.install_retry_seconds,
    )
    # An empty deployment_id means LM reported the app already installed and
    # started no deployment, so there is nothing to poll. The version read-back
    # below is then the only thing that decides success — which is the right
    # authority anyway.
    foreign: list[str] = []
    if deployment_id:
        print(f"install accepted, deployment_id={deployment_id}")
        try:
            _poll_deployment(read_client, deployment_id, args.timeout_seconds)
        except DeploymentFailed as exc:
            diagnostics = _dump_failure(read_client, app_id)
            foreign = foreign_failure(diagnostics, args.image)
            if not foreign:
                raise
            # LM's health check is namespace-scoped, so its FAILED verdict can be
            # about a pod this install never touched — an orphan from an earlier
            # version, which fails every subsequent install to that tenant until
            # someone deletes it. Every failing image here belongs to a different
            # version, so the verdict is not evidence about ours.
            #
            # NOT a pass on its own: the read-back below has to confirm the tenant
            # actually serves the version we installed. Downgrading the verdict
            # only moves the decision to direct evidence — it does not skip it.
            print(
                f"::warning::{exc} — but the failing pod(s) want "
                f"{', '.join(foreign)}, not {args.image}. LM's health check is "
                "namespace-scoped, so an orphaned pod from an earlier version "
                "fails this check for every later install. Falling through to the "
                "installed-version read-back, which decides."
            )
        except TenantAppError:
            # Timeout, or an unrelated failure. Nobody else's fault; fatal.
            _dump_failure(read_client, app_id)
            raise
    else:
        print("no deployment started; relying on the version read-back below")

    installed = _installed_version(read_client, app_id)
    print(f"tenant reports installed version: {installed or '<unreported>'}")
    if foreign and installed != args.version:
        raise TenantAppError(
            f"deployment reported FAILED and the tenant reports "
            f"{installed or '<unreported>'} rather than {args.version}, so the "
            "install did not take effect. The failing pod(s) want "
            f"{', '.join(foreign)} — orphans from an earlier version, which are "
            "worth deleting from the tenant's namespace either way — but with the "
            "read-back disagreeing they are not the whole story here."
        )
    if foreign:
        print(
            f"::notice::tenant serves {installed}; the FAILED verdict was about "
            f"{', '.join(foreign)}, not this install. Those pods should still be "
            "cleaned up — they will fail this check on every future install."
        )
    return InstallOutcome(
        version=args.version,
        version_id=version_id,
        release_id=release_id,
        deployment_id=deployment_id,
        installed_version=installed,
        release_status=status,
    )


def verify(args: argparse.Namespace) -> str:
    """Assert the tenant runs ``--expected``. Returns the installed version."""
    # See install(): resolve from atlan.yaml first, then validate the result.
    app_id = validate_app_id(resolve_app_id(args.app_id))
    base_url = validate_tenant_base_url(args.base_url)
    _, read_client = _clients(base_url)
    installed = _installed_version(read_client, app_id)
    if installed != args.expected:
        raise TenantAppError(
            f"tenant is running {installed or '<nothing / unreported>'} for app "
            f"{app_id}, but this leg tests {args.expected}. Heracles fetches "
            "the DAG from the deployed pod at AE submit, so continuing would "
            "test a different version than the one under test. A concurrent e2e "
            "run against this tenant, or a manual deploy, is the usual cause."
        )
    print(f"verified: tenant runs {installed}")
    return installed


def _write_outputs(outputs: dict[str, str]) -> None:
    """Append to ``$GITHUB_OUTPUT``, or print when running outside Actions.

    No masking pass is needed here, unlike the tenant resolver: every value
    written is an id, a version or a status. No credential reaches this function.
    """
    target = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in outputs.items())
    if not target:
        sys.stdout.write(lines)
        return
    with open(target, "a") as handle:
        handle.write(lines)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True, help="https://<tenant>")
    parser.add_argument(
        "--app-id",
        default="",
        help="GM app UUID. Omit to read app_id from atlan.yaml in the cwd.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="register + install + wait")
    _add_common(p_install)
    p_install.add_argument("--image", required=True)
    p_install.add_argument(
        "--version", required=True, help="GM version (the image tag)"
    )
    p_install.add_argument("--branch", required=True)
    p_install.add_argument(
        "--tenant",
        required=True,
        help=(
            "The tenant's ID for allowed_tenants scoping — its vcluster instance "
            "name (e.g. 'markeznp37'), NOT its hostname. GM matches this exactly; "
            "a hostname yields a release visible to no tenant."
        ),
    )
    p_install.add_argument("--repo-url", default="")
    p_install.add_argument("--deploy-config", default="")
    p_install.add_argument("--self-deployed-runtime", action="store_true")
    p_install.add_argument("--sdk-version", default="")
    p_install.add_argument("--entrypoints", default="")
    p_install.add_argument("--app-configs", default="")
    p_install.add_argument("--release-model", default="")
    p_install.add_argument("--created-by", default="")
    p_install.add_argument(
        "--scan-wait-seconds",
        type=int,
        default=0,
        help=(
            "Seconds to wait for GM's release scan before installing. 0 (the "
            "default) does not wait: a base-image CVE must not red an unrelated "
            "PR's e2e. Raise only if GM refuses to install an unscanned release."
        ),
    )
    p_install.add_argument(
        "--install-retry-seconds",
        type=int,
        default=DEFAULT_INSTALL_RETRY_SECONDS,
        help=(
            "How long to keep retrying the install while LM's tenant-catalog "
            "snapshot catches up with a fresh publish. LM excludes a release "
            "while it is scan_pending and picks it up on the next scheduled sync "
            "(~5 min), so a just-published release is not immediately "
            "installable. 0 disables the retry."
        ),
    )
    p_install.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_DEPLOYMENT_TIMEOUT_SECONDS,
        help="Budget for the deployment to reconcile. A timeout fails.",
    )

    p_verify = sub.add_parser("verify", help="assert the installed version")
    _add_common(p_verify)
    p_verify.add_argument("--expected", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "install":
            outcome = install(args)
            _write_outputs(outcome.as_outputs())
        else:
            _write_outputs({"installed_version": verify(args)})
    except (TenantAppError, TenantApiError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
