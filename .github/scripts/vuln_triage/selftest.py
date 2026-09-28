"""A fake ticket and scan that drive every branch of the triage, built from the live lock.

`--selftest` runs the real pipeline on this fixture instead of a Linear ticket and a scan
artifact: classification, the allowlist edit and its validator, the Case-1 bump with the
real `uv lock` and lock checks, and (unless also a dry run) real draft PRs that are closed
again at the end. It is rebuilt on every run from the checked-out `uv.lock` and
`uv lock --upgrade --dry-run`, so the Case-1 package is always one that is locked at the
scanned version AND has an upgrade uv can resolve. A checked-in fixture would go stale
the day that package was bumped.

The CVE ids are fake (`CVE-SELFTEST-n`) and never reach the shared allowlist: see
run.SELFTEST_BRANCH.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import scan
from .effects import Runner

TICKET = "SELFTEST"
_UPDATE = re.compile(r"^Update (\S+) v(\S+) -> v(\S+)$")


@dataclass
class Fixture:
    ticket: dict[str, Any]
    scan_dir: Path
    summary: str


def registry_packages(lock_text: str) -> dict[str, str]:
    """Locked name -> version, for packages resolved from an index (not path/git)."""
    data = tomllib.loads(lock_text)
    return {
        p["name"]: p["version"]
        for p in data.get("package", [])
        if "registry" in (p.get("source") or {})
    }


def resolvable_upgrades(
    output: str, locked: dict[str, str]
) -> list[tuple[str, str, str]]:
    """(name, locked, newer) from `uv lock --upgrade --dry-run`, registry packages only."""
    out = []
    for line in output.splitlines():
        m = _UPDATE.match(line.strip())
        if m and locked.get(m.group(1)) == m.group(2):
            out.append((m.group(1), m.group(2), m.group(3)))
    return sorted(out)


def _vuln(
    cve: str, pkg: str, installed: str, fixed: str, sev: str, path: str = ""
) -> dict:
    v = {
        "VulnerabilityID": cve,
        "PkgName": pkg,
        "InstalledVersion": installed,
        "FixedVersion": fixed,
        "Severity": sev,
        "Title": "vuln-triage self-test (fake)",
    }
    if path:
        v["PkgPath"] = path
    return v


def fixture_scan(
    upgrades: list[tuple[str, str, str]], locked: dict[str, str]
) -> tuple[list[dict], list[dict], list[str], str]:
    """(fs vulns, image vulns, ticket CVE ids, summary). One CVE per triage branch."""
    fs: list[dict] = []
    notes: list[str] = []
    ids = []
    bumped = ""
    if upgrades:
        bumped, old, new = upgrades[0]
        fs.append(_vuln("CVE-SELFTEST-1", bumped, old, new, "HIGH"))
        ids.append("CVE-SELFTEST-1")
        notes.append(f"case 1 {bumped} {old}->{new}")
    else:
        notes.append("no resolvable upgrade, so no Case-1 bump")
    others = [n for n in sorted(locked) if n != bumped]
    if others:
        nofix = others[0]
        fs.append(_vuln("CVE-SELFTEST-2", nofix, locked[nofix], "", "HIGH"))
        ids.append("CVE-SELFTEST-2")
        notes.append(f"no-fix {nofix}")
    fs.append(
        _vuln(
            "CVE-SELFTEST-3",
            "pyo3",
            "0.20.0",
            "0.24.1",
            "HIGH",
            ".venv/lib/python3.12/site-packages/temporalio/bridge/Cargo.lock",
        )
    )
    ids.append("CVE-SELFTEST-3")
    image = [
        _vuln("CVE-SELFTEST-4", "dapr", "1.16.0", "1.16.2", "CRITICAL", "usr/bin/daprd")
    ]
    ids.append("CVE-SELFTEST-4")
    if len(others) > 1:
        tracked = others[1]
        fs.append(_vuln("CVE-SELFTEST-5", tracked, locked[tracked], "", "MEDIUM"))
        ids.append("CVE-SELFTEST-5")
    ids.append("CVE-SELFTEST-6")  # on the ticket, in no scan: killed
    return fs, image, ids, "; ".join(notes)


def build(root: Path, out_dir: Path, now: datetime, runner: Runner) -> Fixture:
    locked = registry_packages((root / "uv.lock").read_text())
    res = runner(
        ["uv", "lock", "--upgrade", "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
    )
    # uv prints the "Update ..." lines on stderr; read both streams.
    upgrades = resolvable_upgrades(
        (res.stdout or "") + "\n" + (res.stderr or ""), locked
    )
    fs, image, ids, summary = fixture_scan(upgrades, locked)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / scan.FS_FILE).write_text(
        json.dumps({"Results": [{"Target": "uv.lock", "Vulnerabilities": fs}]})
    )
    (out_dir / scan.IMAGE_FILE).write_text(
        json.dumps(
            {"Results": [{"Target": "app-runtime-base", "Vulnerabilities": image}]}
        )
    )
    ticket = {
        "id": "selftest",
        "identifier": TICKET,
        "url": "",
        "createdAt": now.isoformat(),
        "description": "Self-test ticket (fake).\n<!-- vuln-ids: "
        + ",".join(ids)
        + " -->\n",
    }
    return Fixture(ticket=ticket, scan_dir=out_dir, summary=summary)
