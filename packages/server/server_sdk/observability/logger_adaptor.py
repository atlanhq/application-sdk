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


class _UntilRootIsClaimedHandler(logging.StreamHandler):
    """A stderr sink that switches itself off the moment anyone claims root.

    Deferring to application_sdk leaves a window: its InterceptHandler is
    installed by a module-level ``basicConfig`` that runs only when
    ``application_sdk.observability.logger_adaptor`` is IMPORTED, which in the
    host is after app discovery, mount and revision logging -- and the serving
    packages deliberately do not depend on application_sdk, so on a host
    without it nothing imports the bridge at all. Root then has no handler and
    every server_sdk record below WARNING goes to ``lastResort`` or nowhere.

    Emitting only while root is unclaimed keeps both orders correct without
    server_sdk ever owning root: no record is dropped before the bridge
    appears, and nothing is double-printed after it does.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if logging.getLogger().handlers:
            return
        super().emit(record)


def _resolve_level() -> tuple[str, str, bool]:
    """Return the level to use, what was asked for, and whether it was unusable."""
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
    return ("INFO" if unusable else requested), requested, unusable


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    if not _should_own_root():
        # Do not own root, but do not go silent either -- see
        # _UntilRootIsClaimedHandler. The level has to be set on our own logger:
        # root stays at WARNING, so getEffectiveLevel() would filter INFO out
        # before any handler ran.
        level, _requested, _unusable = _resolve_level()
        pkg = logging.getLogger("server_sdk")
        if not any(
            isinstance(h, _UntilRootIsClaimedHandler) for h in pkg.handlers
        ):
            handler = _UntilRootIsClaimedHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
            pkg.addHandler(handler)
        pkg.setLevel(level)
        # Still mark configured: the decision cannot change within a process,
        # and re-probing would cost a find_spec on every get_logger call.
        _CONFIGURED = True
        return
    level, requested, unusable = _resolve_level()
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
    would reintroduce it. The redacted traceback ends up appended to the
    MESSAGE, with both ``exc_info`` and ``exc_text`` cleared, so it survives a
    handler that reads only ``getMessage()`` -- application_sdk's
    InterceptHandler, which owns root in the host, is exactly that -- while a
    structured/JSON/OTel sink still cannot re-derive the unredacted chain from
    the original exception objects.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Render FIRST, then redact the result, then clear args.
        #
        # Redacting the parts instead was wrong twice over. It missed every
        # secret that was not a top-level str or exception — logger.error(exc),
        # or a DSN inside a list or dict arg — which is most of them. And
        # rewriting the FORMAT STRING corrupted it: the secret-param pattern
        # eats a placeholder that follows a secret-named token, so
        # "password=%s was rejected" became "password=***", after which
        # %-formatting raised and the stdlib swallowed the whole record into a
        # "--- Logging error ---" on stderr. Re-tupling args broke the other
        # shape: the stdlib stores a lone Mapping as-is so that "%(user)s"
        # works, and a tuple there raises the same way. A filter that deletes
        # the line it was protecting is worse than the leak.
        try:
            self._redact(record)
            return True
        except Exception:  # noqa: BLE001 — conformance E011.
            # This filter runs on EVERY record in a process serving eight apps,
            # so a raise here would take out logging for all of them. Fail
            # CLOSED: drop the payload rather than pass a record this filter
            # could not prove clean. The line survives as a marker so the loss
            # is visible instead of silent.
            record.msg = "<log record dropped: redaction failed>"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            return True

    @staticmethod
    def _redact(record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad format/arg pair must not
            # become an exception in the logging path; fall back to the raw msg
            # so the record still carries something redacted.
            text = str(record.msg)
        record.msg = redact_secrets(text)
        record.args = None
        if record.exc_info:
            record.exc_text = redact_secrets(
                record.exc_text or "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        # Fold the redacted traceback into the message. exc_text alone only
        # survives a stdlib Formatter; the host gives root to application_sdk's
        # InterceptHandler, which emits record.exc_info and never reads
        # exc_text, so clearing exc_info there deleted the stack outright for
        # every exc_info=True site. Both clears are load-bearing and for
        # different reasons: exc_info so a loguru/OTel sink cannot re-derive the
        # UNREDACTED traceback from the live exception objects, exc_text so
        # logging.Formatter does not then append it a second time.
        if record.exc_text:
            record.msg = f"{record.msg}\n{record.exc_text}"
            record.exc_text = None


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
