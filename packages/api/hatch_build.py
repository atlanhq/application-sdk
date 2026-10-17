"""Package the files listed in ``api-files.txt`` into the api wheel.

The source lives once, under ``application_sdk/`` at the repo root, and ships in
both distributions. The sdist and a regular wheel copy the listed files in; a
wheel built from the unpacked sdist (what ``uv build`` does) finds them next to
this file, a wheel built in the repo reads them from the repo root. An editable
build copies nothing: the SDK's dev env already imports the whole tree from
source, and a copy in site-packages would shadow it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

LIST = "api-files.txt"


def listed_files(root: Path) -> list[str]:
    lines = (root / LIST).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


class ApiFilesHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        here = Path(self.root).resolve()
        repo = here.parent.parent
        for rel in listed_files(here):
            source = here / rel if (here / rel).is_file() else repo / rel
            build_data["force_include"][str(source)] = rel
