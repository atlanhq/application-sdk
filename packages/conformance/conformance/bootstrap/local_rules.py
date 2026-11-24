"""Tell a repository's placeholder mothership review rules from real ones.

The mothership reviewer read a repository's own rules from
``.mothership/review-rulesets/connector-app/``: a ``MANIFEST.yaml`` listing
Markdown files under ``rules/``. Most connector repositories were seeded with
one generic rule there and never wrote another. It says nothing about the
repository, so ``bootstrap`` deletes it with the rest of the mothership kit
(FND-5317). A rule somebody wrote is knowledge about that connector, and it is
moved to ``.github/connector-reviewer/principles/`` by hand. So the whole
folder is left alone unless every rule in it is the placeholder.

The placeholder was the same file in every repository except for two values:
the rule id (``MYSQL-REPO-001``) and the product name in the title
(``# Review MySQL-specific behavior``). A rule counts as the placeholder only
when it equals the text below once those two values and its whitespace are
normalised. Anything else is treated as a real rule, including a placeholder
somebody reworded.
"""

from __future__ import annotations

import pathlib
import re

from conformance.bootstrap.render import LOCAL_REVIEW_RULES_DIR

MANIFEST_NAME = "MANIFEST.yaml"
RULES_DIRNAME = "rules"

_PLACEHOLDER_RULE = """\
---
schema: 2
id: <ID>
level: L3
category: correctness
globs: []
severity: HIGH
suppressible: true
---
# Review <NAME>-specific behavior

- Report a finding only when changed code breaks a concrete repository contract
  shown by code, tests, generated contracts, or maintained documentation.
- Check source-specific discovery, identity, extraction, lineage, or workflow
  behavior only when the diff affects it.
- Do not restate conformance, shared connector, or platform policy covered by
  L1, L2, or L4.
"""

# The two per-repository values. The id must have the placeholder's own shape
# (``<NAME>-REPO-<number>``): a rule given any other id was renamed by hand.
_ID_LINE = re.compile(r"^id:[ \t]*[A-Za-z0-9][A-Za-z0-9_-]*-REPO-\d+[ \t]*$", re.M)
_TITLE_LINE = re.compile(r"^# Review \S.*-specific behavior[ \t]*$", re.M)

# The only lines a placeholder folder's MANIFEST.yaml holds.
_MANIFEST_SCHEMA = re.compile(r"schema:\s*\d+")
_MANIFEST_RULE = re.compile(r"-\s+[\"']?rules/(?P<name>[^/\"'\s]+)[\"']?")


def _normalise(text: str) -> str:
    """Return *text* with the per-repository values and whitespace flattened."""
    text = text.replace("\r\n", "\n")
    text = _ID_LINE.sub("id: <ID>", text, count=1)
    text = _TITLE_LINE.sub("# Review <NAME>-specific behavior", text, count=1)
    return " ".join(text.split())


_PLACEHOLDER_NORMALISED = _normalise(_PLACEHOLDER_RULE)


def is_placeholder_rule(text: str) -> bool:
    """Whether *text* is the generic placeholder rule and nothing more."""
    return _normalise(text) == _PLACEHOLDER_NORMALISED


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _manifest_lists_only(text: str, rule_names: set[str]) -> bool:
    """Whether a MANIFEST.yaml is nothing but a list of *rule_names*."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line == "rules:":
            continue
        if _MANIFEST_SCHEMA.fullmatch(line):
            continue
        listed = _MANIFEST_RULE.fullmatch(line)
        if listed is None or listed.group("name") not in rule_names:
            return False
    return True


def _unrecognised(folder: pathlib.Path) -> list[str]:
    """Names in *folder* that are not its manifest or a file under ``rules/``."""
    odd: list[str] = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        is_manifest = rel.parts == (MANIFEST_NAME,)
        is_rules_dir = rel.parts == (RULES_DIRNAME,) and path.is_dir()
        is_rule = len(rel.parts) == 2 and rel.parts[0] == RULES_DIRNAME
        regular = path.is_file() and not path.is_symlink()
        if is_rules_dir and not path.is_symlink():
            continue
        if (is_manifest or is_rule) and regular:
            continue
        odd.append(rel.as_posix())
    return odd


def placeholder_rule_files(root: pathlib.Path) -> tuple[list[pathlib.Path], str]:
    """Return the local review rules *root* can lose, or why they stay.

    ``(files, "")`` names every file to delete: the placeholder rules and the
    manifest listing them. The list is empty when the folder does not exist.
    ``([], reason)`` means the folder holds something that is not the
    placeholder, so no file in it may be deleted.
    """
    folder = root / LOCAL_REVIEW_RULES_DIR
    # Nothing there, or only empty directories a local checkout kept.
    if not folder.is_dir() or not any(not p.is_dir() for p in folder.rglob("*")):
        return [], ""
    odd = _unrecognised(folder)
    if odd:
        return [], f"it holds files bootstrap does not recognise: {', '.join(odd)}"

    rules_dir = folder / RULES_DIRNAME
    rules = sorted(rules_dir.iterdir()) if rules_dir.is_dir() else []
    if not rules:
        return [], "it holds no rule file to recognise"
    real = [p.name for p in rules if not is_placeholder_rule(_read(p) or "")]
    if real:
        return [], f"it holds rules that are not the placeholder: {', '.join(real)}"

    manifest = folder / MANIFEST_NAME
    if not manifest.exists():
        return rules, ""
    listing = _read(manifest)
    if listing is None or not _manifest_lists_only(listing, {p.name for p in rules}):
        return [], f"its {MANIFEST_NAME} is more than a list of the placeholder rules"
    return [*rules, manifest], ""
