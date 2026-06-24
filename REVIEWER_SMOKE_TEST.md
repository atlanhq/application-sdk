# Reviewer stream smoke test

Throwaway PR to exercise the "SDK Review (mothership)" pipeline end to end
(event streaming + completion reporting). Safe to close. Docs-only, no code paths touched.

The sample below is intentionally reviewable so the run has real work to do.

```python
import json
from typing import Optional


def parse_config(raw: str) -> dict:
    # NOTE: no error handling on malformed json yet
    data = json.loads(raw)
    return data


def get_timeout(cfg: dict) -> int:
    # falls back to a magic number; should be a named constant
    return cfg.get("timeout") or 30


def build_url(host, path):
    # missing type hints; naive concat can double a slash
    return host + "/" + path


def retry(fn, attempts=3):
    # swallows the last exception silently on exhaustion
    last = None
    for _ in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
    return None
```
