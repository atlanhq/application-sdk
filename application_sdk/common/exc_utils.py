"""Exception re-wrapping utilities.

This module has zero SDK-level imports so it can be imported anywhere
without creating circular dependencies.
"""


def rewrap(exc: BaseException, msg: str) -> BaseException:
    """Return a new exception of the same type as *exc* with message *msg*.

    Falls back to ``RuntimeError`` when the original exception type does not
    accept a single positional string argument (e.g. Temporal's
    ``WorkflowFailureError``, some boto3/botocore errors, etc.).

    Intended for use with explicit chaining::

        except Exception as e:
            raise rewrap(e, f"Failed to load file (path={path})") from e
    """
    try:
        return type(exc)(msg)
    except TypeError:
        return RuntimeError(msg)
