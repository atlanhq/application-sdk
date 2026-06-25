"""Meta-tests for the P-series client-seam check (P019, BLDX-1430).

P019 flags raw HTTP (``httpx``/``requests``/``aiohttp``/``urllib``) aimed at an
Atlan service — detected by an ``/api/meta`` (Atlas) or ``/api/service``
(Heracles) marker in the request URL — instead of the supported ``pyatlan``
client.  The rule is a deliberate heuristic, so it is tested to fire across the
request-call shapes that carry a static Atlan URL and, crucially, to stay silent
on the heavy legitimate non-Atlan HTTP the SDK and apps make (Dapr, Segment,
Prometheus, third-party connector APIs) and on blessed ``pyatlan`` construction.
"""

from __future__ import annotations

from conformance.suite.checks.client_seam import SERIES, scan_text


def _rule(src: str, rule_id: str = "P019", file: str = "app/x.py") -> list:
    """Findings of a single rule from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P019 RawHttpToAtlan — fires (raw HTTP carrying an Atlan marker) ──────────


def test_p017_fires_on_httpx_module_get_fstring() -> None:
    src = 'import httpx\nhttpx.get(f"{base}/api/meta/entity/x")\n'
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].line == 2


def test_p017_fires_on_requests_post_string_concat() -> None:
    src = 'import requests\nrequests.post(url + "/api/service/package-workflows")\n'
    assert len(_rule(src)) == 1


def test_p017_fires_on_requests_session_method() -> None:
    src = 'import requests\ns = requests.Session()\ns.get("https://t/api/meta/types/typedefs")\n'
    assert len(_rule(src)) == 1


def test_p017_fires_on_urllib_urlopen() -> None:
    src = 'import urllib.request\nurllib.request.urlopen("https://t/api/service/x")\n'
    assert len(_rule(src)) == 1


def test_p017_fires_on_urllib_request_object() -> None:
    src = 'import urllib.request\nreq = urllib.request.Request("https://t/api/meta/entity")\n'
    assert len(_rule(src)) == 1


def test_p017_fires_on_from_import_function() -> None:
    src = 'from httpx import get\nget("https://x/api/service/native-status")\n'
    assert len(_rule(src)) == 1


def test_p017_fires_on_client_base_url_kwarg() -> None:
    src = 'import httpx\nc = httpx.AsyncClient(base_url="https://x/api/service")\n'
    assert len(_rule(src)) == 1


def test_p017_fires_on_aiohttp_session_method() -> None:
    src = 'import aiohttp\nasync def f(s: aiohttp.ClientSession):\n    await s.get("https://x/api/meta/x")\n'
    assert len(_rule(src)) == 1


def test_p017_fires_on_url_keyword_argument() -> None:
    src = 'import requests\nrequests.request("GET", url="https://x/api/service/y")\n'
    assert len(_rule(src)) == 1


# ── P019 — silent (legitimate non-Atlan HTTP, blessed pyatlan, non-HTTP) ─────


def test_p017_silent_on_dapr_sidecar() -> None:
    src = 'import httpx\nhttpx.post("http://localhost:3500/v1.0/state/statestore")\n'
    assert _rule(src) == []


def test_p017_silent_on_segment_analytics() -> None:
    src = 'import httpx\nhttpx.post("https://api.segment.io/v1/batch")\n'
    assert _rule(src) == []


def test_p017_silent_on_prometheus_pushgateway() -> None:
    src = 'import httpx\nhttpx.get("http://127.0.0.1:9464/metrics")\n'
    assert _rule(src) == []


def test_p017_silent_on_third_party_connector_api() -> None:
    src = 'import httpx\nhttpx.get("https://api.github.com/repos/x/y")\n'
    assert _rule(src) == []


def test_p017_silent_on_urllib_parse_helpers() -> None:
    # urllib.parse is string manipulation, not an HTTP request — never flagged,
    # even when the joined path is an Atlan path.
    src = 'from urllib.parse import urljoin\nu = urljoin(base, "/api/service/x")\n'
    assert _rule(src) == []


def test_p017_silent_on_pyatlan_construction() -> None:
    # Constructing pyatlan's own client IS reuse of the blessed dependency.
    src = (
        "from pyatlan_v9.client.aio import AsyncAtlanClient\n"
        "client = AsyncAtlanClient(base_url=u, api_key=k)\n"
    )
    assert _rule(src) == []


def test_p017_silent_on_atlan_marker_without_http_import() -> None:
    # A bare ``.get`` with an Atlan-looking key but no raw-HTTP surface imported
    # (e.g. a dict/config lookup) must not be flagged.
    src = 'config.get("/api/service/x")\n'
    assert _rule(src) == []


def test_p017_silent_on_http_without_atlan_marker() -> None:
    src = 'import httpx\nhttpx.get(f"{base}/v1/users")\n'
    assert _rule(src) == []


# ── Suppression ──────────────────────────────────────────────────────────────


def test_p017_suppressed_inline() -> None:
    src = (
        "import httpx\n"
        'httpx.get("https://x/api/meta/x")  # conformance: ignore[P019] low-level e2e probe\n'
    )
    fs = _rule(src)
    assert len(fs) == 1 and fs[0].suppressed
