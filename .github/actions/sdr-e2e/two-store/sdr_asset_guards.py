#!/usr/bin/env python3
"""Decoupled SDR asset guards — run as a post-workflow step in the sdr-e2e action.

Reads the bind-mounted object stores on the HOST and asserts the SDR extraction
landed assets in the deployment store and (opt-in) reached the upstream store.

Why here and not in ``BaseSDRIntegrationTest``: the SDK-side guard only runs for
an app pinned to an SDK version that CONTAINS it — i.e. it forces an SDK bump on
every SDR app before the guard can fire. This host-side check has **no dependency
on the app's installed SDK** — it only inspects the extraction output on disk
(which the two-store overlay bind-mounts). So it runs on any of the SDR apps at
their CURRENT SDK version (redshift 3.14, looker/saperp 3.17, …) with no bump.

Env (set by the two-store block in action.yaml):
  SDR_REQUIRE_ASSETS_LANDED           enforce deployment-store landing
  SDR_REQUIRE_UPSTREAM_ASSETS_LANDED  enforce upstream-store landing
  SDR_EXTRACTED_OUTPUT_BASE_PATH      deployment base (e.g. data/artifacts/apps/<app>/workflows)
  SDR_UPSTREAM_OUTPUT_BASE_PATH       upstream base   (e.g. data-upstream/artifacts/apps/<app>/workflows)
  SDR_OUTPUT_SUBDIR                   output subdir to assert on (default "transformed")
"""

from __future__ import annotations

import glob
import json
import os
import sys


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _count(base: str, subdir: str) -> tuple[list[str], int]:
    """Return (data files, total record count) under {base}/**/{subdir}/**.

    Globs defensively: the connector writes to {base}/{workflow_id}/{run_id}/
    {subdir}/, but we don't need the exact ids — any populated {subdir} under
    the base means assets landed.
    """
    files: list[str] = []
    for pat in (
        os.path.join(base, "*", "*", subdir, "**", "*.json"),
        os.path.join(base, "**", subdir, "**", "*.json"),
    ):
        files.extend(glob.glob(pat, recursive=True))
    # de-dupe, keep real files, drop sha256 sidecars from the count of "assets"
    files = sorted({f for f in files if os.path.isfile(f)})
    records = 0
    for f in files:
        try:
            with open(f) as fh:
                txt = fh.read().strip()
            if not txt:
                continue
            if txt.lstrip().startswith("["):
                records += len(json.loads(txt))  # JSON array
            else:
                records += sum(1 for ln in txt.splitlines() if ln.strip())  # JSONL
        except (json.JSONDecodeError, OSError) as e:
            print(f"::warning::SDR asset guard — unparseable data file {f}: {e}")
    return files, records


def _assert(kind: str, base: str, subdir: str) -> bool:
    if not base:
        print(f"::error::SDR {kind} guard: base path env not set")
        return False
    files, records = _count(base, subdir)
    if not files or records == 0:
        print(
            f"::error::SDR {kind} guard FAILED — no assets under {base}/**/{subdir} "
            f"(files={len(files)}, records={records}). This is the classic SDR "
            f"silent-success / 0-asset (or wrong-location) failure."
        )
        return False
    print(
        f"SDR {kind} guard OK — {records} record(s) across {len(files)} file(s) "
        f"under {base}/**/{subdir}"
    )
    return True


def main() -> None:
    subdir = os.environ.get("SDR_OUTPUT_SUBDIR", "transformed")
    ok = True
    ran = False
    if _flag("SDR_REQUIRE_ASSETS_LANDED"):
        ran = True
        ok = (
            _assert(
                "deployment",
                os.environ.get("SDR_EXTRACTED_OUTPUT_BASE_PATH", ""),
                subdir,
            )
            and ok
        )
    if _flag("SDR_REQUIRE_UPSTREAM_ASSETS_LANDED"):
        ran = True
        ok = (
            _assert(
                "upstream", os.environ.get("SDR_UPSTREAM_OUTPUT_BASE_PATH", ""), subdir
            )
            and ok
        )
    if not ran:
        print("SDR asset guards not enabled (no SDR_REQUIRE_* flags) — skipping.")
        return
    if not ok:
        sys.exit(1)
    print("SDR asset guards passed.")


if __name__ == "__main__":
    main()
