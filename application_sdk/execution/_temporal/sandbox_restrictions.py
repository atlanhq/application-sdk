"""Temporal-specific adjustments to the workflow sandbox's restrictions.

Kept in the Temporal adapter so ``application_sdk.execution.sandbox`` does not
depend on temporalio's matcher API.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterable

from temporalio.worker.workflow_sandbox import SandboxMatcher, SandboxRestrictions

#: ``os.path`` members that only manipulate strings: no filesystem, working
#: directory or environment read, so they return the same value on replay on
#: any worker. Everything else in ``os.path`` (``exists``, ``isdir``,
#: ``getmtime``, ``abspath``, ``realpath``, ``relpath``, ``expanduser``, ...)
#: stays refused in workflow code.
PURE_OS_PATH_MEMBERS: frozenset[str] = frozenset(
    {
        "basename",
        "commonpath",
        "commonprefix",
        "dirname",
        "isabs",
        "join",
        "normcase",
        "normpath",
        "split",
        "splitdrive",
        "splitext",
        "splitroot",
        # Constants, in case the matcher treats reading them as a use.
        "altsep",
        "curdir",
        "defpath",
        "devnull",
        "extsep",
        "pardir",
        "pathsep",
        "sep",
    }
)


def build_restrictions(passthrough_modules: Iterable[str]) -> SandboxRestrictions:
    """Temporal's default restrictions, plus passthrough and the os.path rule.

    Args:
        passthrough_modules: Module names to pass through the sandbox.

    Returns:
        The restrictions the SDK's workflow runner uses.
    """
    restrictions = SandboxRestrictions.default.with_passthrough_modules(
        *passthrough_modules
    )
    return allow_pure_os_path(restrictions)


def allow_pure_os_path(restrictions: SandboxRestrictions) -> SandboxRestrictions:
    """Allow the pure ``os.path`` functions in workflow code, on every Python.

    temporalio refuses all of ``os.path`` on Python < 3.14 and allows all of
    it on 3.14+ (CPython needs ``isabs`` and ``join`` there to render
    tracebacks). Left alone, workflow code that builds a path with
    ``os.path.join`` passes on a 3.14 laptop and fails its workflow task on
    the 3.13 production image, while ``os.path.exists`` on 3.14 lets workflow
    code branch on worker-local files. This applies one rule on every
    version: the string-only members in :data:`PURE_OS_PATH_MEMBERS` are
    allowed, and the rest of ``os.path`` is refused at workflow runtime.

    Args:
        restrictions: The restrictions to adjust.

    Returns:
        A copy of ``restrictions`` with the ``os.path`` rule replaced, or
        ``restrictions`` unchanged if it does not restrict ``os`` at all.
    """
    members = restrictions.invalid_module_members
    os_matcher = members.children.get("os")
    if os_matcher is None:
        return restrictions
    path_matcher = SandboxMatcher(
        use={"*"},
        exclude=set(PURE_OS_PATH_MEMBERS),
        only_runtime=True,
    )
    # temporalio refuses every use of os ("*") with only a few names
    # excluded, and on 3.14+ "path" is one of them. Either way the os-level
    # rule decides os.path before its child matcher is consulted, so spell out
    # the same rule as the explicit list of os's public names, minus "path",
    # and let the child matcher above decide os.path member by member.
    exclude = frozenset(os_matcher.exclude) - {"path"}
    use = set(os_matcher.use)
    if "*" in use:
        use = {name for name in dir(os) if not name.startswith("_")}
    os_matcher = dataclasses.replace(
        os_matcher,
        use=use - exclude - {"path"},
        children={**os_matcher.children, "path": path_matcher},
        exclude=exclude,
    )
    members = dataclasses.replace(
        members, children={**members.children, "os": os_matcher}
    )
    return dataclasses.replace(restrictions, invalid_module_members=members)
