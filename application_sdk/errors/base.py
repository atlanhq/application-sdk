"""AppError — canonical SDK exception base (kw-only dataclass)."""

from __future__ import annotations

import dataclasses
import re
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from application_sdk.errors.categories import Audience, FailureCategory

if TYPE_CHECKING:
    from application_sdk.errors.wire import FailureDetails

# Fields present on every AppError — excluded from the wire `evidence` dict.
_BASE_FIELDS: frozenset[str] = frozenset(
    {"message", "retryable", "cause", "app_name", "run_id", "suggested_action"}
)

# Cap for the cause string carried on the wire envelope. Sized so a full
# provider error response survives intact. An object-store error's ``str()`` is
# "[preamble + request URL][provider XML/JSON body][Rust ``Debug source:`` dump]"
# — measured at ~800 chars for a typical artifact key. The previous 500 spent
# 374 on the URL preamble alone and cut the provider's ``<Message>`` in half,
# deleting the only sentence that named what the store rejected. (FND-957)
_CAUSE_MAX_LEN = 2000
# Past the cap, keep BOTH ends rather than the head only: a backend error puts
# the request URL at the head and what the provider said at the tail, so a
# head-only cut spends the whole budget on boilerplate. The two lengths sum to
# less than _CAUSE_MAX_LEN so the elision marker only appears when there is
# genuinely something elided.
_CAUSE_HEAD_LEN = 1200
_CAUSE_TAIL_LEN = 700
_TRACEBACK_MAX_LEN = 8000
#: Recursion bound for :func:`redact_wire_value`. A pathologically deep
#: hand-built structure must truncate rather than overflow the stack.
_REDACT_MAX_DEPTH: int = 32
# Matches URL userinfo of any shape — ``user:pass@``, a bare token as the whole
# userinfo (git remotes, registries, webhook URLs), an empty user with a
# password (``redis://:pw@``) — up to the ``@`` that ends it, which must come
# before any ``/`` or whitespace, so an ``@`` in a path or query string is never
# userinfo. A password containing ``@`` is taken whole: further ``xxx@`` runs
# before the first ``/`` belong to the same userinfo. The one exemption lives in
# :func:`_sub_userinfo`: in the Azure blob schemes ``container@account`` is
# addressing, not a credential, and is left alone while no password is present.
# The negative lookbehind makes the scheme start at a token boundary — without
# it the engine, refused at ``abfss://``, retries at ``bfss://`` and redacts the
# container anyway. (An earlier cut required a ``:`` in the userinfo instead;
# that dropped the bare-token class the greedy form had always covered.)
_URL_USERINFO_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])([a-z][a-z0-9+.-]*://)((?:[^@\s/]*@)+)", re.IGNORECASE
)
_STRUCTURAL_USERINFO_SCHEMES = frozenset({"abfss", "abfs", "wasbs", "wasb"})


def _sub_userinfo(m: re.Match[str]) -> str:
    """Replacement for :data:`_URL_USERINFO_RE`: redact, unless it is Azure addressing."""
    scheme, userinfo = m.group(1), m.group(2)
    if scheme[:-3].lower() in _STRUCTURAL_USERINFO_SCHEMES and ":" not in userinfo:
        return m.group(0)
    return f"{scheme}***@"


# Matches secret query params: api_key=value → api_key=***
# ``pwd`` covers ODBC/DSN keyword syntax (``UID=sa;PWD=…``), which no other
# keyword here matches — ODBC connectors do not use ``password=``.
# The value alternation tries a braced value first: ODBC quotes values that
# contain the ``;`` separator as ``PWD={secret;with;semicolons}``, and the
# bare-value class ``[^\s&,;#]+`` would stop at the first ``;`` inside the
# braces and leak the password tail. ``\{[^}]*\}`` consumes the braces as a
# unit so only the closing brace survives; an *escaped* closing brace
# (``}}`` per the ODBC spec) still ends the match at the first ``}`` — the
# residue is then a brace fragment, not usable secret material. The bare
# class still stops at ``;``, so the following key=value pair survives.
# ``uid`` is deliberately absent. It is a user name, not a credential, and
# dropping it would remove "which account failed to log in" from every auth
# failure. It also has no word boundary in this alternation, so it would match
# the tail of ``run_guid=`` and ``correlation_uuid=`` — redacting the exact
# correlation IDs an on-call needs.
# A suffix-matching ``token`` is deliberately NOT here, for the same reason
# ``uid`` is not: it would redact ``next_token=`` / ``page_token=`` /
# ``continuation_token=``, the pagination cursors an on-call needs to see.
# The list stays an enumeration of things that are only ever credentials.
# ``signature`` and ``sig`` cover presigned object-store URLs, which object-store
# errors quote verbatim in their message: AWS SigV4 ``X-Amz-Signature``, GCS
# ``X-Goog-Signature``, and Azure SAS ``sig``. ``credential`` already matched
# ``X-Amz-Credential`` (an access-key id plus scope), but the signature is the
# part that actually authorises the request, so redacting only the credential
# left the usable half in the string. ``sig`` carries a lookbehind because it is
# short enough to appear as the tail of a longer word; the other tokens are
# distinctive enough not to need one.
#
# Inside an absolute URL none of this matters: :data:`_URL_QUERY_FRAGMENT_RE`
# redacts every query value, cursors included. This list only governs bare
# ``key=value`` text with no URL around it (connection strings, DSNs, prose),
# where a cursor is still worth keeping. ``token`` and ``auth`` therefore carry
# the same lookbehind as ``sig``: they match the bare name but not the tail of
# ``next_token`` / ``page_token`` / ``oauth``. ``code`` is deliberately absent —
# bare, it is far more often ``status code=500`` than an OAuth code.
# The leading ``\*\*\*`` alternative leaves a value the URL pass already
# redacted alone, so the quote that closes ``'https://h/p?token=***'`` survives;
# it only applies when ``***`` is the whole value, so it can never stop short
# of a real secret. The negative lookahead after ``=`` likewise leaves a quoted
# value :data:`_QUOTED_SECRET_RE` already masked (``password='***'``) intact.
_SECRET_NAMES = r"(?:api_key|apikey|api-key|access_token|auth_token|refresh_token|id_token|password|passwd|pwd|secret|credential|private_key|signature|sharedaccesskey|accountkey|(?<![a-z0-9_])(?:sig|token|auth))"
# A credential-bearing HTTP header. Matched as a suffix like the names above, so
# ``proxy-authorization``, ``set-cookie``, ``x-api-key`` and ``x-auth-token``
# are covered by ``authorization``, ``cookie``, ``api-key`` and ``auth-token``.
_SECRET_HEADER_NAMES = r"(?:authorization|cookie|api-key|auth-token)"
# The first letters of every name in the two lists above. ``re`` cannot
# prefilter an alternation that holds a lookbehind, so without this it tries
# every name at every offset; the lookahead rejects most offsets in one step,
# which made the quoted pass ~6x faster on a 20 KB traceback. A name added to
# either list must start with one of these letters
# (``test_every_secret_name_passes_the_first_letter_gate`` checks).
_SECRET_NAME_START = r"(?=[aiprsct])"
_SECRET_PARAM_RE = re.compile(
    r"(?i)(" + _SECRET_NAME_START + _SECRET_NAMES + r"=)(?!(['\"])\*\*\*\2)"
    r"(?:\*\*\*(?=['\"<>]?(?:[\s&,;#]|$))|\{[^}]*\}|[^\s&,;#]+)",
)
# An ``Authorization`` scheme, kept in front of the masked credential so the
# line still says how the request authenticated.
_AUTH_SCHEME = r"(?:(?:bearer|basic|token)\s+)?"
# Where a header name or ``Bearer`` may start: a token boundary, or just after
# an escaped ``\n`` / ``\r`` / ``\t``. A message rendered through ``repr()``
# (loguru's ``diagnose`` annotations, an exception repr) puts each header on
# the same physical line after a literal ``\n``, whose ``n`` would otherwise
# fail the boundary.
_TOKEN_START = r"(?:(?<![a-z0-9_-])|(?<=\\[nrt]))"
# A quoted value under a secret name: a dict or JSON entry (``'password': 'x'``,
# ``"api_key":"x"``), httpx's raw header pairs (``(b'cookie', b'x')``) or a
# keyword repr (``password='x'``). The value is masked up to its closing quote,
# so a value holding a space, ``,`` or ``;`` — where the bare pass would stop —
# cannot leak a tail, and the entries after it survive. Escapes are honoured
# (``'it\'s'``); an unterminated value is masked to the end of the line. A
# quoted key has its closing quote right after the name, so ``'next_token'``
# and ``'token_type'`` keep the same boundaries as the bare pass. An unquoted
# key needs ``=``: ``secret: 'x'`` is as likely prose naming a secret as a
# secret, and a header line with an unquoted name is
# :data:`_SECRET_HEADER_LINE_RE`'s.
_QUOTED_SECRET_RE = re.compile(
    r"(?im)("
    + _SECRET_NAME_START
    + "(?:"
    + _SECRET_NAMES
    + "|"
    + _SECRET_HEADER_NAMES
    + r")"
    r"(?:['\"]\s*[:,]|=)\s*b?(['\"])" + _AUTH_SCHEME + r")"
    r"(?:\\.|(?!\2)[^\\\n])*(\2|$)",
)
# A raw ``Name: value`` header line. A header value runs to the end of the line,
# and a cookie's has spaces and ``;`` in it, so everything after the name (and
# any ``Authorization`` scheme) is masked; in a ``repr()``'d message that is
# the rest of the repr, which over-masks rather than leaks. The lookahead
# straight after ``:`` leaves an already-masked value alone; placed after the
# optional spaces and scheme instead, the engine would backtrack them to empty
# and re-mask ``Authorization: ***`` as ``Authorization:***``.
_SECRET_HEADER_LINE_RE = re.compile(
    r"(?im)("
    + _TOKEN_START
    + r"(?:proxy-authorization|authorization|set-cookie|cookie|x-api-key|api-key|x-auth-token)"
    r"[ \t]*:(?![ \t]*" + _AUTH_SCHEME + r"\*\*\*)[ \t]*" + _AUTH_SCHEME + r")[^\r\n]+",
)
# A ``Bearer <token68>`` with no header name around it, as exception text
# quotes it. ``token`` itself is skipped so ``invalid bearer token`` stays
# readable.
_BEARER_TOKEN_RE = re.compile(
    r"(?i)(" + _TOKEN_START + r"bearer\s+)(?!\*\*\*|token\b)[a-z0-9._~+/-]+=*",
)
# An absolute URL's query and/or fragment: everything from the first ``?`` or
# ``#`` up to the end of the URL. The URL ends at whitespace, a quote or an angle
# bracket, which covers the ways a URL is quoted inside an error message or a
# traceback source line. The scheme boundary is the one :data:`_URL_USERINFO_RE`
# uses.
_URL_QUERY_FRAGMENT_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])([a-z][a-z0-9+.-]*://[^\s?#'\"<>]*)([?#][^\s'\"<>]*)",
    re.IGNORECASE,
)
# Query params are split on ``;`` as well as ``&``: some servers accept either,
# and splitting on ``&`` alone would let an allowlisted ``page=2;owner=x`` carry
# the unknown ``owner`` value through whole.
_QUERY_SEPARATOR_RE = re.compile(r"([&;])")
# ANSI CSI escapes (loguru's colorized console). Stripped before matching: a
# colour code ending in ``m`` glued to ``https://`` defeats the scheme-boundary
# lookbehind, and a code inside a URL would end the match early.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


# Query params whose value is kept. An allowlist, so an unknown name is redacted
# by default; add a name only if its value can never be a credential. The Azure
# SAS fields say which resource, which permissions and which validity window a
# URL had (``se`` is what tells an on-call the SAS expired); the rest are
# pagination. These values reach the ``FailureDetails`` envelope a person
# reads, not only logs.
_SAFE_QUERY_PARAMS = frozenset({"sv", "se", "sr", "sp", "limit", "page", "offset"})


def _redact_query_param(param: str) -> str:
    """``name=value`` → ``name=***``; a value-less param could be a bare token."""
    name, sep, value = param.partition("=")
    if sep:
        if not value or name.lower() in _SAFE_QUERY_PARAMS:
            return param
        return f"{name}=***"
    return "***" if param else param


def _sub_query_fragment(m: re.Match[str]) -> str:
    """Replacement for :data:`_URL_QUERY_FRAGMENT_RE`: keep param names, drop values."""
    tail = m.group(2)
    query, hash_, fragment = tail.partition("#")
    if query.startswith("?"):
        query = "?" + "".join(
            part if part in ("&", ";") else _redact_query_param(part)
            for part in _QUERY_SEPARATOR_RE.split(query[1:])
        )
    if hash_:
        query += "#***" if fragment else "#"
    return m.group(1) + query


def redact_secrets(text: str) -> str:
    """Redact credentials from a string bound for a log or the wire.

    ANSI colour escapes are stripped first, then these passes, in order:

    * URL userinfo (``scheme://user:pass@``) → ``scheme://***@``, except Azure
      blob ``container@account`` addressing.
    * Every query value and the whole fragment of any absolute URL →
      ``?owner=***&token=***#***``, except values of the few params in
      :data:`_SAFE_QUERY_PARAMS` (pagination, Azure SAS scope and expiry).
      Param names stay so the line is still diagnosable. The URL ends at
      whitespace, a quote or ``<``/``>``.
    * Quoted values under a credential name or header in a dict, JSON or
      keyword repr → ``{'password': '***'}``, ``password='***'``,
      ``{'Authorization': 'Bearer ***'}``, masked up to the closing quote.
    * Raw credential header lines (``Authorization``, ``Proxy-Authorization``,
      ``Cookie``, ``Set-Cookie``, ``X-Api-Key``, ``Api-Key``, ``X-Auth-Token``)
      → ``Authorization: Bearer ***``, masked to the end of the line.
    * A bare ``Bearer <token>`` → ``Bearer ***``.
    * Bare ``key=value`` pairs outside a URL whose key is a known credential
      name (``password``, ``token``, ``apikey``, ``pwd``, ``sig``, …).

    Why a URL query is redact-by-default but bare text is redact-by-name: a
    URL's ``?`` marks exactly where its params start, so every value can be
    masked and only known-harmless ones kept. Free text has no such marker —
    ``status code=500``, ``attempt=3``, ``run_guid=…`` and ``next_token=…`` are
    diagnostics, not secrets — so outside a URL only names that are always
    credentials can be masked without wiping every message.

    Known limits: a secret in a URL *path* (``/keys/<token>/…``) is not
    redacted, nor is a URL with no ``scheme://`` (``host/p?token=x``) or one
    escaped inside JSON (``https:\\/\\/…``); bare pairs under an unlisted key
    name survive, as does a non-string value (``'auth': ('u', 'pw')``), a JSON
    object escaped inside a string (``{\\"password\\": …}``) and a secret in
    free text with no recognisable key. Redaction is a denylist of shapes:
    defence in depth, not a guarantee. Use it as a backstop, not a licence to
    log raw payloads. loguru's ``diagnose`` stays off by default for the same
    reason: with it on, every local is rendered and only these shapes are
    masked.

    Use this when logging strings that may embed credentials but are not a
    single cause exception — e.g. a formatted traceback whose frames are worth
    keeping but whose driver messages embed connection-string passwords.

    ``text`` must be a ``str`` — callers holding an exception or other object
    should stringify first (the sibling :func:`sanitize_cause_repr` does this
    for cause exceptions). Non-``str`` input raises ``TypeError`` via ``re``.
    """
    if "\x1b" in text:
        text = _ANSI_ESCAPE_RE.sub("", text)
    text = _URL_USERINFO_RE.sub(_sub_userinfo, text)
    text = _URL_QUERY_FRAGMENT_RE.sub(_sub_query_fragment, text)
    text = _QUOTED_SECRET_RE.sub(r"\1***\3", text)
    text = _SECRET_HEADER_LINE_RE.sub(r"\1***", text)
    text = _BEARER_TOKEN_RE.sub(r"\1***", text)
    text = _SECRET_PARAM_RE.sub(r"\1***", text)
    return text


def redact_wire_value(value: Any, seen: set[int] | None = None, depth: int = 0) -> Any:
    """Redact every string reachable inside a value bound for the wire.

    Strings are redacted wherever they appear inside dict / list / tuple / set
    structures; other non-string values are left untouched. The recursive
    counterpart to :func:`redact_secrets`, which handles one string.

    Args:
        value: Any value bound for a wire — typically a ``model_dump`` result or
            an evidence mapping.
        seen: Container ids on the current recursion path. Callers pass nothing;
            the walker threads it through itself.
        depth: Current recursion depth. Callers pass nothing.

    Returns:
        The same shape with every reachable string redacted. A revisited
        container yields ``None`` and anything past :data:`_REDACT_MAX_DEPTH`
        yields ``"…"``, so a hostile structure truncates rather than hangs.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        if seen is None:
            seen = set()
        # Guard the two ways a hand-built container can crash the walk:
        #   * a self-referential container recurses forever — prune a revisit
        #     rather than render it;
        #   * a pathologically deep acyclic structure overflows the stack —
        #     bound depth and truncate past it.
        # ``seen`` is a mutable add/remove recursion stack shared along the path,
        # so diamond-shared (acyclic) subcontainers are redacted, not falsely
        # pruned, and no frozenset is allocated per level.
        if id(value) in seen:
            return None
        if depth >= _REDACT_MAX_DEPTH:
            return "…"
        seen.add(id(value))
        try:
            return {k: redact_wire_value(v, seen, depth + 1) for k, v in value.items()}
        finally:
            seen.discard(id(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        if seen is None:
            seen = set()
        if id(value) in seen:
            return None
        if depth >= _REDACT_MAX_DEPTH:
            return "…"
        seen.add(id(value))
        try:
            redacted = [redact_wire_value(v, seen, depth + 1) for v in value]
        finally:
            seen.discard(id(value))
        if isinstance(value, tuple) and hasattr(type(value), "_fields"):
            # A NamedTuple takes positional fields, not an iterable —
            # ``type(value)(redacted)`` crashes a 2+-field one and silently
            # retypes a 1-field one (its sole field becomes the redacted
            # *list*). Rebuild with positional expansion so the shape survives.
            try:
                return type(value)(*redacted)
            except Exception:  # noqa: BLE001 — see fallback below
                return tuple(redacted)
        try:
            return type(value)(redacted)
        except Exception:  # noqa: BLE001 — a connector-authored container
            # subclass whose constructor raises (any type, not just
            # TypeError/ValueError) must not crash through the degrade either;
            # fall back to a plain container of the same shape. Values are
            # redacted either way, and evidence serialises as JSON arrays
            # regardless.
            return tuple(redacted) if isinstance(value, tuple) else redacted
    return value


# ``object_store`` (via obstore) appends a multi-line Rust ``Debug`` dump to
# every error's ``str()``. It sits *after* the provider's XML/JSON body, so it
# competes with the diagnostic for the budget — and a keep-the-tail truncation
# would preserve the dump and drop the body. Measured on a real GCS write
# failure: [374 chars URL+status][206 chars provider XML][218 chars Debug dump].
# Strip it before capping; the routing-relevant facts reach consumers as typed
# evidence instead. (FND-957)
_DEBUG_SOURCE_TAIL_RE = re.compile(r"\n+Debug source:\n.*\Z", re.DOTALL)


def redact_and_cap(text: str) -> str:
    """Redact secrets in ``text``, then cap it, keeping both ends.

    The one cap block: :func:`sanitize_cause_repr` builds on it for cause
    exceptions, and the preflight outcome rows call it on the handler's own
    ``message`` before it becomes a log attribute. Not re-exported from
    ``application_sdk.errors`` — those are its two consumers.

    Truncation keeps both ends because a backend error puts the request URL at
    the head and what the provider said at the tail; a head-only cut spends the
    budget on boilerplate. Redaction runs *before* truncation, so retaining a
    tail can never expose an unredacted secret. (FND-957)
    """
    text = redact_secrets(text)
    if len(text) > _CAUSE_MAX_LEN:
        elided = len(text) - _CAUSE_HEAD_LEN - _CAUSE_TAIL_LEN
        text = (
            text[:_CAUSE_HEAD_LEN]
            + f"…[{elided} chars elided]…"
            + text[-_CAUSE_TAIL_LEN:]
        )
    return text


def sanitize_cause_repr(exc: BaseException) -> str:
    """Return a length-capped, secret-redacted string for a cause exception.

    Truncation keeps both ends. A backend error puts the request URL at the
    head and the reason at the tail, so a head-only cut spends the whole
    budget on boilerplate and deletes the diagnostic. Redaction runs *before*
    truncation, so retaining a tail can never expose an unredacted secret.
    (FND-957)
    """
    text = _DEBUG_SOURCE_TAIL_RE.sub("", str(exc))
    return f"{type(exc).__name__}: {redact_and_cap(text)}"


def safe_traceback(exc: BaseException | None, max_len: int = _TRACEBACK_MAX_LEN) -> str:
    """Return a secret-redacted, length-capped full-frame traceback.

    For logging a traceback whose frames are worth keeping but whose driver
    messages may embed connection-string passwords. Redacts URL userinfo and
    known secret params, then caps the total length with an ellipsis marker.
    Returns ``""`` for ``None``; an exception never raised (no ``__traceback__``)
    yields just its formatted type/message line.
    """
    if exc is None:
        return ""
    text = redact_secrets("".join(traceback.format_exception(exc)))
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


# Backward-compat alias: the helper is load-bearing across clients/sql.py and
# credentials/errors.py, so it is public. Kept for existing internal/test imports.
_sanitize_cause_repr = sanitize_cause_repr


@dataclass(kw_only=True)
class AppError(Exception):
    """Canonical SDK exception base.

    Subclass one of the categorical leaves (AuthError, AppNotFoundError, …)
    to define a typed error. Add dataclass fields to carry structured
    evidence — they appear automatically in ``to_failure_details()``.
    """

    message: str
    retryable: bool | None = None
    cause: BaseException | None = None
    app_name: str | None = None
    run_id: str | None = None
    suggested_action: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.INTERNAL
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "INTERNAL"
    audience: ClassVar[Audience] = Audience.APP_OWNER

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)
        if self.cause is not None and self.__cause__ is None:
            self.__cause__ = self.cause

    def __str__(self) -> str:
        return self.message

    @property
    def effective_retryable(self) -> bool:
        """Per-instance retryable, falling back to class default."""
        return self.default_retryable if self.retryable is None else self.retryable

    @property
    def qualified_code(self) -> str:
        """``CATEGORY.CODE`` string for log lines and human-readable surfaces."""
        return f"{self.category.name}.{self.code}"

    def to_failure_details(self) -> FailureDetails:
        """Build the Pydantic wire envelope from this error's dataclass fields.

        Non-base fields become ``evidence``. The Error dataclass is the schema
        source — no separate model to keep in sync.

        Tenant identity is intentionally NOT included here. The producer
        (the failing app) does not know or carry tenant context; per-tenant
        attribution is the consumer's responsibility (the Automation Engine
        or another consumer reading ``ApplicationError.details`` attaches
        tenant from its own context at ingest time).
        """
        from application_sdk.errors.wire import FailureDetails  # noqa: PLC0415

        evidence: dict[str, Any] = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.name not in _BASE_FIELDS
        }
        return FailureDetails(
            category=self.category,
            code=self.code,
            retryable=self.effective_retryable,
            audience=type(self).audience,
            message=self.message,
            suggested_action=self.suggested_action,
            evidence=evidence,
            app_name=self.app_name,
            run_id=self.run_id,
            cause_repr=sanitize_cause_repr(self.cause) if self.cause else None,
        )
