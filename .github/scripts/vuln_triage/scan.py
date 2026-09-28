"""Read the scan's Trivy JSON and the SDK's uv.lock into plain data."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_FILE = "trivy-image-results.json"
FS_FILE = "trivy-fs-results.json"

# `.venv/lib/python3.12/site-packages/temporalio/bridge/Cargo.lock` -> temporalio. A
# `*.dist-info` directory is the Python package itself, not something vendored inside it.
_SITE_PACKAGES_DIR = re.compile(r"site-packages/([^/]+)/")


def normalize(name: str) -> str:
    """PEP 503 name normalisation, so `Foo_Bar` in Trivy matches `foo-bar` in uv.lock."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Hit:
    """One package a CVE was found in, by one of the two scans."""

    package: str
    installed: str
    fixed: str  # "" when there is no fix
    source: str  # "fs" (our tree + installed .venv) or "image" (app-runtime-base)
    target: str  # Trivy's Target, or the package path when it has one
    title: str = ""
    url: str = ""

    @property
    def vendored_in(self) -> str:
        """The installed wheel this package ships inside (a Rust Cargo.lock in a wheel),
        or "" when it is a top-level package."""
        m = _SITE_PACKAGES_DIR.search(self.target)
        if not m or m.group(1).endswith((".dist-info", ".egg-info")):
            return ""
        return m.group(1)


@dataclass
class Finding:
    cve: str
    severity: str
    hits: list[Hit] = field(default_factory=list)


class ScanIncomplete(RuntimeError):
    """The scan artifact lacks one of the two Trivy results."""


def load_findings(scan_dir: Path) -> dict[str, Finding]:
    """Every CVE in both scans, with each package it was found in.

    Both results files are required. The scan uploads its artifact even when a scan
    step failed, and a missing file must not read as "no findings": every ticket CVE
    only that scanner reports would then be killed as cleared."""
    missing = [f for f in (FS_FILE, IMAGE_FILE) if not (scan_dir / f).is_file()]
    if missing:
        raise ScanIncomplete(
            f"scan artifact in {scan_dir} is missing {', '.join(missing)}"
        )
    findings: dict[str, Finding] = {}
    for fname, source in ((FS_FILE, "fs"), (IMAGE_FILE, "image")):
        data = json.loads((scan_dir / fname).read_text())
        for result in data.get("Results") or []:
            for v in result.get("Vulnerabilities") or []:
                cve = v.get("VulnerabilityID")
                if not cve:
                    continue
                f = findings.setdefault(
                    cve, Finding(cve=cve, severity=(v.get("Severity") or "").upper())
                )
                hit = Hit(
                    package=v.get("PkgName", ""),
                    installed=v.get("InstalledVersion", ""),
                    fixed=(v.get("FixedVersion") or "").strip(),
                    source=source,
                    target=v.get("PkgPath") or result.get("Target", ""),
                    title=v.get("Title", ""),
                    url=v.get("PrimaryURL", ""),
                )
                if hit not in f.hits:
                    f.hits.append(hit)
    return findings


def load_lock(path: Path) -> dict[str, dict]:
    """uv.lock packages by normalised name: {"version": ..., "upload_time": ...}.

    The upload time is the sdist's (or the first wheel's) — what the cooldown check reads.
    """
    data = tomllib.loads(path.read_text())
    out: dict[str, dict] = {}
    for pkg in data.get("package", []):
        upload = (pkg.get("sdist") or {}).get("upload-time", "")
        if not upload and pkg.get("wheels"):
            upload = pkg["wheels"][0].get("upload-time", "")
        out[normalize(pkg["name"])] = {
            "version": pkg.get("version", ""),
            "upload_time": upload,
        }
    return out


def lock_registry_hosts(text: str) -> set[str]:
    """Every host a uv.lock points at — a bump must never introduce a new one."""
    return set(re.findall(r'(?:registry|url) = "https?://([^/"]+)', text))
