"""
Reads the JSON file list emitted by dorny/paths-filter (list-files: json) from
the SDK_FILES environment variable and writes sdk=true|false to $GITHUB_OUTPUT.

sdk=true  when at least one matched file is not a Markdown file.
sdk=false when every matched file is Markdown (or the list is empty).

Always exits 0. Called by the 'Refine sdk — suppress markdown-only matches'
step in pull_request.yaml and sdk-gate.yaml.
"""

import json
import os

files = json.loads(os.environ.get("SDK_FILES", "[]"))
has_non_md = any(not f.endswith(".md") for f in files)

github_output = os.environ.get("GITHUB_OUTPUT", "")
line = "sdk=" + ("true" if has_non_md else "false")

if github_output:
    with open(github_output, "a") as fh:
        fh.write(line + "\n")
else:
    print(line)
