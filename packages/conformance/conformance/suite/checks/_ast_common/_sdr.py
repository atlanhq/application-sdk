"""Whether a repo is a self-deployed-runtime (SDR) app.

The SDR rules (``sdr``), the SDR test-coverage rules (``sdr_test_checks``) and
the e2e workflow-shape rules (``e2e_workflow_shape``) all gate on this one
answer, so it lives here: three copies could drift into three definitions of
"SDR app".
"""

from __future__ import annotations

import re
from pathlib import Path

from ._io import safe_read_text

__all__ = ["is_sdr_app"]

_SDR_FLAG_RE = re.compile(
    r"^self_deployed_runtime:\s*(true|false)\b",
    re.MULTILINE | re.IGNORECASE,
)


def is_sdr_app(root: Path) -> bool:
    """True when ``root/atlan.yaml`` declares ``self_deployed_runtime: true``.

    An absent, unreadable or undecodable ``atlan.yaml`` is not an SDR app.
    """
    atlan_yaml = root / "atlan.yaml"
    if not atlan_yaml.is_file():
        return False
    text = safe_read_text(atlan_yaml)
    if text is None:
        return False
    m = _SDR_FLAG_RE.search(text)
    return m is not None and m.group(1).lower() == "true"
