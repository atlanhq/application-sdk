"""Collecting what a failed run left behind, and redacting it before it ships.

A red CI leg is only useful if the evidence for it survives the pod that
produced it. This module collects that evidence into one bundle — pod logs,
the DAG node table, the counts that were read, the findings — and redacts it on
the way out.

Redaction is not optional and not a caller's responsibility. The harness handles
credential bodies (a connector's source credentials, an Atlan API key, a
tenant hostname) and evidence is the one thing here that is *designed* to leave
the process. Redaction therefore happens at this boundary, not by withholding
fields from the types upstream: withholding makes the domain objects harder to
debug locally while still leaking anything a future field forgets to withhold.
:func:`write_bundle` redacts unconditionally, so there is no ordering a caller
can get wrong.

**Two filters, because one of them cannot see enough.**

*By key name.* A credential in structured data is a value under a
credential-shaped key, so :data:`SECRET_KEY_FRAGMENTS` is matched as a
case-insensitive substring of the key and the value is replaced wholesale. In
text this is the ``key=value`` / ``key: value`` / ``"key": "value"`` forms, all
three of which appear in the same container log.

*By literal value.* The prior art in this repo is explicit about why the first
filter is not enough on its own: ``resolve_e2e_tenant.py`` and
``export_extra_env.py`` both run a two-pass ``--mask-only`` protocol precisely
because registering a blob as a secret does not redact the values *inside* it.
A token that a driver echoed back with no key beside it — in a URL path, in an
exception message, as a bare header dump — is invisible to key matching. So
:func:`redact` also takes the literal values the run is holding and blanks them
wherever they appear. A caller that has the API key should pass it.

**Over-redaction is the intended failure direction, with exactly one exception.**
:func:`~application_sdk.errors.base.redact_secrets` — which this module also
applies, for URL userinfo — argues the opposite for ``uid``: it is a user name,
not a credential, and dropping it removes "which account failed to log in" from
every auth failure. That reasoning holds *there*, on an error message whose
blast radius is one string an on-call reads. It does not transfer here, where the
output is uploaded and retained. So the fragment list is broad, and the one
deliberate omission is a bare ``auth``: it would take ``auth_type`` with it, and
``auth_type`` is the field that says which credential shape a connector was
configured for — the first thing read on a credential-routing failure.
``authorization`` and ``auth_token`` are matched explicitly instead.

**A collector may fail open. Nothing else here may.** The ban on
empty-result-on-error (FND-224's C4) is about readings that get *graded*: an
unreadable count must not be scored as a low count. An evidence dump is never
graded — it is read after the verdict — so a collector that raised would turn a
diagnosable failure into an undiagnosable one. Every collection failure is
logged and the rest of the collection still runs. Redaction is the opposite: it
has no failure mode that is allowed to pass content through, so it is pure and
total.

**What this module does not do is decide when to collect.** A bundle is built by
whoever knows the run failed. On the connector path that is the e2e base class,
which writes into a directory the CI job already uploads — no workflow change,
because ``results/`` is the path ``upload-artifact`` is already pointed at.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import orjson

from application_sdk.errors.base import redact_secrets
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.cluster.kube import KubernetesReader
from application_sdk.testing.harness.expectations import Finding

logger = get_logger(__name__)

__all__ = [
    "PLACEHOLDER",
    "SECRET_KEY_FRAGMENTS",
    "EvidenceBundle",
    "collect_pod_evidence",
    "redact",
    "redact_text",
    "secrets_from_environment",
    "write_bundle",
]

#: What a redacted value is replaced with. Matches
#: :func:`~application_sdk.errors.base.redact_secrets`, so a string that passed
#: through both carries one marker rather than two spellings of it.
PLACEHOLDER = "***"

#: Key-name substrings that make a value credential-shaped, matched
#: case-insensitively. Substrings rather than whole words so ``x-api-key``,
#: ``AWS_SECRET_ACCESS_KEY`` and ``clientSecret`` all hit without an entry each.
#: See the module docstring for why a bare ``auth`` is deliberately absent.
SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "access_key",
    "accesskey",
    "api_key",
    "api-key",
    "apikey",
    "authorization",
    "auth_token",
    "certificate",
    "client_secret",
    "clientsecret",
    "cookie",
    "credential",
    "passphrase",
    "passwd",
    "password",
    "private_key",
    "privatekey",
    "pwd",
    "secret",
    "session_id",
    "signature",
    "token",
)

#: Shortest literal value worth blanking. A one- or two-character "secret" is
#: either not one or is a substring of half the log, and blanking it would
#: destroy the evidence to protect nothing. Three is where a value starts being
#: specific enough that its appearance is not a coincidence.
_MIN_LITERAL_LENGTH = 3

#: HTTP authentication schemes whose *next* token is the credential. Without
#: these, ``Authorization: Bearer eyJ...`` redacts the word ``Bearer`` and ships
#: the token — the value class below stops at the first space, which is right for
#: everything else and exactly wrong here.
_AUTH_SCHEMES = ("Basic", "Bearer", "Digest", "Negotiate", "Token", "APIKey")

#: ``key=value``, ``key: value`` and ``"key": "value"``. Three notes, each of
#: which is a bug that was there before it:
#:
#: * The key is anchored on a non-word character (or the start), so ``run_id=``
#:   is not matched by a fragment that is a substring of ``id`` and an operator
#:   keeps the correlation ids a report is navigated by.
#: * The separator absorbs a closing quote, so the JSON form is covered by the
#:   same pattern as the bare one. They appear in the same container log.
#: * The value stops at whitespace or a separator so the *next* pair in a
#:   connection string stays readable, with three exceptions consumed whole: a
#:   quoted value, an ODBC ``{braced}`` one whose ``;`` would otherwise end the
#:   match early, and an auth scheme plus its token.
#:
#: The quoted alternatives consume backslash escapes (``(?:[^"\\]|\\.)*``)
#: rather than stopping at the first quote. A plain ``"[^"]*"`` truncates at an
#: **escaped** quote, so ``"password": "part\"secret"`` would redact to
#: ``"password": "***"secret"`` — the tail written into an uploaded artifact.
#: JSON is where this arrives: any credential containing a double quote is
#: escaped by every serialiser that produced the log in the first place. The two
#: branches are disjoint (the class excludes the backslash the other requires),
#: so the alternation cannot backtrack catastrophically.
#:
#: The key's surrounding classes are bounded rather than ``*`` for the same
#: reason: the pattern cannot backtrack quadratically over a long unbroken run
#: of word characters, and a container log is exactly where one turns up.
_KEYED_VALUE_RE = re.compile(
    r"(?i)(?<![\w-])([\w.-]{0,64}(?:"
    + "|".join(re.escape(fragment) for fragment in SECRET_KEY_FRAGMENTS)
    + r")[\w.-]{0,64})([\"']?\s*[:=]\s*)"
    + r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\{[^}]*\}|(?:"
    + "|".join(_AUTH_SCHEMES)
    + r")\s+\S+|[^\s,;&#]+)"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceBundle:
    """Everything worth keeping about one harness run.

    Attributes:
        label: What the run was — the suite and entrypoint, for the report title.
        findings: Every unmet expectation, accumulated rather than truncated at
            the first.
        logs: Source name -> captured lines. Source is a pod name, a container,
            or a synthetic name for a non-pod source.
        readings: Named observations the run made — asset counts, node states,
            poller identities. Kept as a mapping rather than typed per kind so a
            new observation does not need a new field here.
        artifacts: Relative path -> file contents to write alongside the report.
    """

    label: str
    findings: Sequence[Finding] = field(default_factory=tuple)
    logs: Mapping[str, Sequence[str]] = field(default_factory=dict)
    readings: Mapping[str, object] = field(default_factory=dict)
    artifacts: Mapping[str, str] = field(default_factory=dict)


def redact_text(text: str, *, secrets: Sequence[str] = ()) -> str:
    """Return *text* with credential-shaped and literally-known values blanked.

    Three passes, in the order that matters: literal values first (they may sit
    inside something a later pass would only partially rewrite), then the
    keyed-value forms, then
    :func:`~application_sdk.errors.base.redact_secrets` for URL userinfo and the
    SDK's own secret query-params.

    Args:
        text: The text to sanitise.
        secrets: Literal values to blank wherever they appear, regardless of
            what surrounds them. Values shorter than three characters are
            ignored — see :data:`_MIN_LITERAL_LENGTH`.

    Returns:
        The sanitised text. Idempotent: :data:`PLACEHOLDER` contains no key
        fragment and no literal, so redacting twice changes nothing.
    """
    # Sorted here, not only in `secrets_from_environment`. Ordering is a
    # *correctness* property of the substitution — replacing a short literal
    # that prefixes a longer one turns `tok-abcdef` into `***-abcdef` and ships
    # the tail — so it belongs in the function doing the replacing rather than
    # in one of the several things that can produce the sequence. `redact` and
    # `write_bundle` take any `Sequence[str]`, and a caller passing a plain list
    # has no reason to know the order matters.
    for literal in sorted(secrets, key=len, reverse=True):
        if len(literal) >= _MIN_LITERAL_LENGTH:
            text = text.replace(literal, PLACEHOLDER)
    text = _KEYED_VALUE_RE.sub(_blank_keyed_value, text)
    return redact_secrets(text)


def _blank_keyed_value(match: re.Match[str]) -> str:
    """Replace a matched value, keeping the quotes that made it a JSON string.

    A quoted value comes back quoted. Without that, redacting an artifact that
    happens to be JSON produces ``"apiKey": ***``, which no longer parses — and
    a bundle whose machine-readable half stopped being machine-readable at the
    redaction step is a strictly worse artefact than one with a masked value in
    it.
    """
    key, separator, value = match.groups()
    quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
    blanked = f"{value[0]}{PLACEHOLDER}{value[0]}" if quoted else PLACEHOLDER
    return f"{key}{separator}{blanked}"


def redact(bundle: EvidenceBundle, *, secrets: Sequence[str] = ()) -> EvidenceBundle:
    """Return *bundle* with credential-shaped values replaced by placeholders.

    Args:
        bundle: The bundle to sanitise.
        secrets: Literal values to blank wherever they appear. The API key, the
            source credentials, anything the run is holding — see the module
            docstring on why key-name matching alone is not enough.

    Returns:
        A new bundle. Never mutates the input: a caller that logs locally and
        uploads remotely must be able to hold both, and an in-place scrub makes
        the local copy useless.
    """
    return replace(
        bundle,
        label=redact_text(bundle.label, secrets=secrets),
        findings=tuple(
            Finding(
                subject=redact_text(finding.subject, secrets=secrets),
                detail=redact_text(finding.detail, secrets=secrets),
                # Not redacted: the expectation is one of a closed set of
                # markers a report groups on, and passing it through the text
                # filter would let a future marker named e.g. "token_shape"
                # come out as "***" and stop matching UNREADABLE.
                expectation=finding.expectation,
            )
            for finding in bundle.findings
        ),
        logs={
            redact_text(source, secrets=secrets): tuple(
                redact_text(line, secrets=secrets) for line in lines
            )
            for source, lines in bundle.logs.items()
        },
        readings=_redact_mapping(bundle.readings, secrets=secrets),
        artifacts={
            redact_text(path, secrets=secrets): redact_text(body, secrets=secrets)
            for path, body in bundle.artifacts.items()
        },
    )


def _redact_mapping(
    mapping: Mapping[str, object], *, secrets: Sequence[str]
) -> Mapping[str, object]:
    """Blank every value under a credential-shaped key, recursively.

    Whole-value replacement, not a text rewrite: in structured data the key
    already establishes that the value is a credential, so there is nothing to
    preserve inside it and every reason not to try. A nested mapping under such
    a key is replaced entire — a credential *body* is exactly that shape, and
    descending into it to redact key by key would pass through any sub-key the
    fragment list has not seen.
    """
    redacted: dict[str, object] = {}
    for key, value in mapping.items():
        if _is_secret_key(key):
            redacted[redact_text(key, secrets=secrets)] = PLACEHOLDER
        else:
            redacted[redact_text(key, secrets=secrets)] = _redact_value(
                value, secrets=secrets
            )
    return redacted


def _redact_value(value: object, *, secrets: Sequence[str]) -> object:
    """Walk a reading, keeping its shape and sanitising its leaves.

    Numbers and booleans pass through as themselves. That is deliberate: the
    readings mapping is where asset counts and node states live, and stringifying
    them here would make the bundle's own JSON unusable for the thing it exists
    to answer. Anything that is not a container, a string, or a number is
    rendered with :func:`repr` and then sanitised — an exception, an enum, a
    dataclass all reach here, and a bundle that dropped them would be quieter and
    less useful.
    """
    if isinstance(value, Mapping):
        return _redact_mapping(
            {str(key): item for key, item in value.items()}, secrets=secrets
        )
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Sequence):
        return [_redact_value(item, secrets=secrets) for item in value]
    return redact_text(repr(value), secrets=secrets)


def _is_secret_key(key: str) -> bool:
    """Is *key* credential-shaped? Case-insensitive substring match."""
    lowered = key.lower()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def secrets_from_environment(
    environ: Mapping[str, str], *, also: Sequence[str] = ()
) -> tuple[str, ...]:
    """Collect the literal values a run is holding, for :func:`redact`'s ``secrets``.

    The same move ``resolve_e2e_tenant.py`` and ``export_extra_env.py`` make with
    ``::add-mask::``, and for the same reason: a value that appears in a log with
    no key beside it — echoed inside a URL path, quoted in a driver's exception,
    dumped as a bare header — is invisible to key-name matching, and the only
    filter that catches it is one that knows the value.

    Every variable whose *name* is credential-shaped contributes its *value*, so
    the two filters share one definition of what looks like a credential and a
    fragment added to :data:`SECRET_KEY_FRAGMENTS` strengthens both at once.

    Args:
        environ: Environment to read. Passed in rather than read from
            :data:`os.environ` so a test can drive it without mutating process
            state.
        also: Extra variable names to contribute regardless of their shape. The
            tenant base URL belongs here: its name is not credential-shaped and
            its value is not a credential, but a tenant hostname identifies a
            customer environment and an evidence bundle is retained and shared.

    Returns:
        The distinct non-blank values, longest first. Order is load-bearing:
        :func:`redact_text` substitutes literally, so a value that is a prefix of
        another must not be replaced first — doing so leaves the longer one's
        tail behind, which is a partial secret in a file that claims to have
        none.
    """
    names = [name for name in environ if _is_secret_key(name)] + list(also)
    values = {environ[name].strip() for name in names if environ.get(name, "").strip()}
    return tuple(sorted(values, key=len, reverse=True))


def write_bundle(
    bundle: EvidenceBundle, output_dir: Path, *, secrets: Sequence[str] = ()
) -> Sequence[Path]:
    """Redact *bundle* and write it under *output_dir*.

    Redaction happens here rather than being asked of the caller, because this
    is the boundary the bundle crosses: on the connector CI path *output_dir*
    sits inside the directory ``upload-artifact`` is already pointed at, so
    anything written here is published. Calling :func:`redact` first is safe —
    the filters are idempotent.

    Writes ``report.json`` (the label, findings and readings, machine-readable),
    one ``logs/{source}.log`` per log source, and each artifact at its own
    relative path. Split rather than one blob because that is how they are read:
    a person opens one container's log, a script parses the report.

    **Every file is UTF-8**, unconditionally and regardless of the writing
    machine's locale — so anything reading one back must say so rather than
    relying on the platform default, which is cp1252 on Windows. Worth stating
    as a contract rather than leaving as an implementation detail: a bundle
    routinely carries a connector name, a driver's error message and a pod's log
    line, and a reader that decodes those with the wrong codec produces mojibake
    in the one artefact whose whole job is to be trusted after the fact.

    Args:
        bundle: What to write.
        output_dir: Directory to write into. Created if absent.
        secrets: Literal values to blank — see :func:`redact`.

    Returns:
        The paths written, in the order they were written. Empty when the
        directory could not be created, which is logged and not raised: an
        evidence dump that failed must not become the failure being diagnosed.
    """
    sanitised = redact(bundle, secrets=secrets)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning(
            "harness evidence: could not create %s — the bundle for %r is not "
            "being written, but the run's own verdict is unaffected",
            output_dir,
            sanitised.label,
            exc_info=True,
        )
        return ()

    written: list[Path] = []
    report = {
        "label": sanitised.label,
        "findings": [
            {
                "subject": finding.subject,
                "detail": finding.detail,
                "expectation": finding.expectation,
            }
            for finding in sanitised.findings
        ],
        "readings": sanitised.readings,
    }
    written.extend(
        _write_one(
            output_dir / "report.json",
            # `default=str` rather than a raise: a reading that survived
            # `_redact_value` as a non-JSON type would otherwise cost the whole
            # report, and the report is the file a script reads.
            orjson.dumps(
                report,
                default=str,
                option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
            ).decode(),
        )
    )
    for source, lines in sanitised.logs.items():
        written.extend(
            _write_one(
                output_dir / "logs" / f"{_as_filename(source)}.log", "\n".join(lines)
            )
        )
    for relative_path, body in sanitised.artifacts.items():
        written.extend(_write_one(output_dir / _as_relative(relative_path), body))
    logger.info(
        "harness evidence: wrote %d file(s) for %r under %s",
        len(written),
        sanitised.label,
        output_dir,
    )
    return tuple(written)


def _write_one(path: Path, body: str) -> Sequence[Path]:
    """Write one file, reporting rather than raising on failure.

    Explicit UTF-8 with ``errors="replace"``: ``write_text`` otherwise uses the
    locale encoding, which is cp1252 on Windows and cannot represent most of
    what a container logs — and this is evidence, so one undecodable character
    must not cost the whole file.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", errors="replace")
    except OSError:
        logger.warning(
            "harness evidence: could not write %s — the rest of the bundle is "
            "still being written",
            path,
            exc_info=True,
        )
        return ()
    return (path,)


def _as_filename(source: str) -> str:
    """Flatten a log-source name into one path segment.

    A source is a pod or container name and can legitimately carry a ``/``
    (``pod/container``); left alone it would silently become a directory level,
    so two sources differing only there would land on top of each other.
    """
    return source.replace("/", "-").replace("\\", "-") or "unnamed"


def _as_relative(relative_path: str) -> Path:
    """Confine an artifact path to *output_dir*.

    An artifact's key is a relative path, and a caller assembling one from a pod
    or container name can produce ``..`` or a leading ``/`` without meaning to.
    Neither is rejected — an evidence write must not fail on a naming quirk —
    but both are flattened, so a bundle can never write outside the directory it
    was handed.
    """
    parts = [
        part
        for part in Path(relative_path).parts
        if part not in ("..", "/", "\\") and not Path(part).is_absolute()
    ]
    return Path(*parts) if parts else Path("unnamed")


async def collect_pod_evidence(
    namespace: str,
    *,
    reader: KubernetesReader,
    label_selector: str = "",
    tail_lines: int = 10_000,
) -> EvidenceBundle:
    """Collect container logs and pod state from *namespace* into one bundle.

    Every pod's current container output, plus the *previous* container's output
    wherever ``restartCount > 0`` — which is where a crash loop's actual cause
    is, and the one thing a merged log stream cannot express.

    Typed as the concrete
    :class:`~application_sdk.testing.harness.cluster.KubernetesReader` rather
    than as :class:`~application_sdk.testing.harness.cluster.ClusterReader`
    because it needs the per-container read that Protocol deliberately does not
    carry, for exactly that reason.

    Best-effort throughout: an unreadable listing yields an empty bundle rather
    than an exception, and one unreadable container costs that container's log
    and nothing else. See the module docstring for why this is the one thing
    here allowed to fail open.

    Args:
        namespace: Namespace to collect from.
        reader: Cluster reader to list pods and read their logs with.
        label_selector: Narrows which pods are collected, in ``kubectl -l``
            syntax. Empty collects every pod in the namespace.
        tail_lines: Cap on lines per container.

    Returns:
        A bundle whose :attr:`~EvidenceBundle.logs` are keyed
        ``{pod}/{container}`` (and ``{pod}/{container}/previous`` for a restarted
        container's earlier output), and whose
        :attr:`~EvidenceBundle.readings` carry one entry per pod: phase,
        readiness, restart total and node. **Not yet redacted** — redaction is
        :func:`write_bundle`'s, at the boundary that ships it.
    """
    label = f"pods in {namespace}" + (
        f" matching {label_selector}" if label_selector else ""
    )
    try:
        pods = list(await reader.pods(namespace, label_selector))
    except Exception:
        logger.warning(
            "harness evidence: could not list pods in %s — returning an empty "
            "bundle rather than raising, so the verdict this was collected for "
            "survives",
            namespace,
            exc_info=True,
        )
        return EvidenceBundle(label=label)

    reads = [
        (f"{pod.name}/{container}", pod.name, container, False)
        for pod in pods
        for container in (pod.containers or {})
    ] + [
        (f"{pod.name}/{container}/previous", pod.name, container, True)
        for pod in pods
        for container, restarts in (pod.containers or {}).items()
        if restarts > 0
    ]

    async def _read(pod: str, container: str, previous: bool) -> Sequence[str]:
        try:
            text = await reader.container_log(
                namespace, pod, container, previous=previous, tail_lines=tail_lines
            )
        except Exception:
            logger.warning(
                "harness evidence: could not read %slogs for %s/%s/%s — the rest "
                "of the collection continues",
                "previous " if previous else "",
                namespace,
                pod,
                container,
                exc_info=True,
            )
            return ()
        return tuple(text.splitlines())

    collected = await asyncio.gather(
        *(_read(pod, container, previous) for _, pod, container, previous in reads)
    )
    return EvidenceBundle(
        label=label,
        logs={key: lines for (key, *_), lines in zip(reads, collected, strict=True)},
        readings={
            pod.name: {
                "phase": pod.phase.value,
                "ready": pod.ready,
                "restarts": pod.restarts,
                "node": pod.node,
            }
            for pod in pods
        },
    )
