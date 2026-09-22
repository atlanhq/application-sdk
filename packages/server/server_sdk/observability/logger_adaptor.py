"""Minimal logger adaptor.

A single ``get_logger`` entry point backed by stdlib logging — no OpenTelemetry
and no structured-log machinery on the serving path.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import traceback

from server_sdk.errors.redaction import redact_secrets  # noqa: E402

_CONFIGURED = False


def _should_own_root() -> bool:
    """Whether this package should configure the root logger at all.

    It is a library, and ``logging.basicConfig`` is a process-global mutation
    that is a NO-OP once root has a handler. So whoever runs first wins — and
    in the consolidated host that is always server_sdk, because the first entry
    point loaded imports it before anything imports application_sdk.

    The cost is invisible: application_sdk's ``basicConfig`` installs its
    stdlib->loguru InterceptHandler, which is what stamps app_name /
    deployment_name / source onto every stdlib record and fans records out to
    the structured stdout formatter, the OTLP exporter and the object-store log
    sink. Claiming root here silently disables all of that for the whole host.

    So: defer when someone already owns root, and defer when application_sdk is
    merely INSTALLED, because its adaptor is the richer one and should win
    whichever import order happens. ``find_spec`` does not import it.
    """
    if logging.getLogger().handlers:
        return False
    return importlib.util.find_spec("application_sdk") is None


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    if not _should_own_root():
        # Still mark configured: the decision cannot change within a process,
        # and re-probing would cost a find_spec on every get_logger call.
        _CONFIGURED = True
        return
    # ATLAN_LOG_LEVEL is the primary name -- it is what the fleet sets, and
    # application_sdk reads it first -- with LOG_LEVEL as the fallback. Reading
    # only LOG_LEVEL meant a tenant raising the level got no effect at all.
    requested = (
        os.getenv("ATLAN_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
    ).upper()
    # basicConfig raises on an unknown level, and this runs at import of any
    # server_sdk module -- so one typo in a chart value would crashloop the whole
    # host, every app with it, rather than degrading one log line.
    unusable = requested not in logging.getLevelNamesMapping()
    level = "INFO" if unusable else requested
    # Exactly one basicConfig call: it configures the root logger only the first
    # time, so a warning emitted before it would fix the level at WARNING.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if unusable:
        logging.getLogger(__name__).warning(
            "Ignoring unusable log level %r; falling back to INFO.", requested
        )
    _CONFIGURED = True


class _RedactingFilter(logging.Filter):
    """Redact secrets from the whole record, traceback included.

    The wire is scrubbed at the envelope, but the log is a second, separate
    sink -- pod stderr, which for tenant vclusters ships to ClickHouse. Three
    ways a credential got there:

    * the format string itself;
    * an ``%s`` operand that is the raw driver exception (``str(exc)`` embeds
      the DSN);
    * ``exc_info=True``, which formats the whole chain from the ORIGINAL
      exception objects -- so redacting the operand, as an earlier fix did,
      left the password one line below on the traceback.

    A filter rather than discipline at ~25 call sites: the next ``exc_info``
    would reintroduce it. ``exc_info`` is cleared once folded into
    ``exc_text``, because a structured/JSON/OTel handler re-derives the
    traceback from ``exc_info`` and would otherwise bypass the redaction.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not lose the line
            text = f"{record.msg!r} % {record.args!r}"
        record.msg = redact_secrets(text)
        record.args = None
        if record.exc_info:
            record.exc_text = redact_secrets(
                record.exc_text or "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        return True


_REDACTING_FILTER = _RedactingFilter()


def get_logger(name: str) -> logging.Logger:
    """Return a configured stdlib logger that redacts secrets."""
    _configure()
    logger = logging.getLogger(name)
    # On the LOGGER, not a handler: _configure() uses basicConfig, which is a
    # no-op once root already has handlers (uvicorn configures first under the
    # host), so a handler we install may never be attached. Logger-level
    # filters run in Logger.handle regardless of who owns the handlers.
    if not any(isinstance(f, _RedactingFilter) for f in logger.filters):
        logger.addFilter(_REDACTING_FILTER)
    return logger
