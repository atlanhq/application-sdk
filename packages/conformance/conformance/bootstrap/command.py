"""``bootstrap`` command: write the SKILL.md shim and standard CI workflows.

Split out of ``conformance.cli`` so that module stays a thin dispatcher (every
other subcommand there is a 3-6 line delegator into its own module) — this
was by far the largest and most actively-changed subcommand. Argv parsing
lives in ``conformance.bootstrap.args`` and flag autodetection lives in
``conformance.bootstrap.autodetect``; this module is the orchestrator plus
the actual write-phase helpers (``_bootstrap_file``, the self-guard, and the
``_sync_*`` functions for the write-if-absent scaffolds).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tomllib
from collections.abc import Callable

from conformance.bootstrap.args import BOOTSTRAP_USAGE, parse_bootstrap_args
from conformance.bootstrap.autodetect import apply_bootstrap_autodetection
from conformance.bootstrap.extract import (
    extract_renovate_automerge,
    extract_tests_yaml_params,
    format_dropped_declarations,
    strip_action_pins,
    unpreserved_tests_yaml_declarations,
)
from conformance.bootstrap.render import (
    MANAGED_ACTION_FILES,
    MANAGED_CONNECTOR_REVIEW_FILES,
    MANAGED_WORKFLOWS,
    RETIRED_CONNECTOR_REVIEW_FILES,
    RETIRED_FILES,
    render,
)

_PACKAGE_NAME = "atlan-application-sdk-conformance"

# Statuses that count as "this path was written" for --json's touched_files
# manifest (see main()). Everything else (an "exists"/"unchanged" no-op) is
# reported under `unchanged` instead. "removed" counts: a deletion is a change
# to that path like any write, so it has to fall inside the pass's declared
# write scope (see remediate-finding.prose.md's touched_files note) — both to
# be committed and to be reverted if a later gate rejects the fix.
_TOUCHED_STATUSES = frozenset(
    {"installed", "updated", "scaffolded", "backed_up", "removed"}
)


def _bootstrap_file(
    dest: pathlib.Path, content: str, *, executable: bool = False
) -> str:
    """Write *content* to *dest*, creating parent directories as needed.

    Always-overwrite-managed — bootstrap owns these files and re-running is
    how drift is eradicated — but a no-op write when *content* already
    matches what's on disk prints ``ok (up to date)`` instead of ``updated``.
    This matters beyond cosmetics: ``touched_files`` (see
    ``remediate-finding.prose.md``) is derived from which paths print an
    ``installed:``/``updated:``/``backed up:`` prefix here, and only an
    actually-changed path should count as touched by a given remediation
    pass — an unconditional ``updated:`` on every re-run would make every
    bootstrap-based fix report all managed files as touched, not just the
    one(s) that actually drifted.

    Returns ``"installed"``, ``"updated"``, or ``"unchanged"`` — the same
    classification the printed prefix encodes, but structured for ``main()``
    to fold into the ``--json`` touched-files manifest without a caller
    having to re-derive it from stdout text.
    """
    expected_mode = 0o755
    if dest.exists():
        try:
            unchanged = dest.read_text(encoding="utf-8") == content
        except (OSError, UnicodeDecodeError):
            unchanged = False
        mode_matches = not executable or (dest.stat().st_mode & 0o777) == expected_mode
        if unchanged and mode_matches:
            print(f"ok (up to date): {dest}")
            return "unchanged"
        if not unchanged:
            dest.write_text(content, encoding="utf-8")
        if executable:
            os.chmod(dest, expected_mode)
        print(f"updated: {dest}")
        return "updated"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    if executable:
        os.chmod(dest, expected_mode)
    print(f"installed: {dest}")
    return "installed"


def _retire_file(dest: pathlib.Path) -> str:
    """Delete *dest* if present — the counterpart to ``_bootstrap_file``.

    Bootstrap wrote every ``RETIRED_WORKFLOWS`` name into every consumer repo,
    so retiring one has to actively remove those copies: dropping the template
    alone would leave each repo's copy in place, still firing on every PR.
    Deleting here (rather than in a one-shot fleet script) means the retirement
    rides the same always-overwrite re-run that installed the file, and cannot
    be undone by the next resync.

    Returns ``"removed"`` or ``"absent"``, mirroring ``_bootstrap_file``'s
    written/no-op classification for ``main()``'s ``--json`` manifest.
    """
    if not dest.exists():
        return "absent"
    dest.unlink()
    print(f"removed: {dest}  (retired managed file)")
    return "removed"


def _is_inside_conformance_repo(start: pathlib.Path) -> bool:
    """Detect whether *start* is anywhere inside the atlan-application-sdk-conformance
    package's own source checkout.

    Walks upward from *start* (rather than checking *start* itself) so the
    detection holds regardless of which subdirectory bootstrap is invoked
    from — the repo root, inside packages/conformance/ itself, or any other
    subdirectory in between.

    Keyed on ``packages/conformance/pyproject.toml`` naming this exact
    package (``atlan-application-sdk-conformance``), not merely on a
    directory named ``packages/conformance`` existing — a bare directory-name
    check would silently no-op the entire bootstrap write phase (exit 0, no
    scaffolding installed) in any consumer monorepo that happens to contain
    an unrelated ``packages/conformance/`` path.
    """
    for candidate in (start, *start.parents):
        pyproject = candidate / "packages" / "conformance" / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if data.get("project", {}).get("name") == _PACKAGE_NAME:
            return True
    return False


def _sync_tests_yaml(
    root: pathlib.Path, kwargs: dict[str, str], resync: bool
) -> list[tuple[pathlib.Path, str]]:
    """tests.yaml — write-if-absent scaffold; apps customise freely.

    C002 tracks drift at WARN only.  With *resync* (``--resync``) an existing
    file is re-rendered from the canonical template so a repo scaffolded by an
    older bootstrap catches up structurally.

    The re-render deliberately ignores *kwargs* and reads its params back off
    the existing file instead.  kwargs' tests.yaml values come from flags and
    autodetection, neither of which reflects what this file actually says:
    ``enable_e2e`` is never autodetected at all (it defaults to ``"true"``),
    so rendering from kwargs would silently switch e2e back on in a repo that
    turned it off, and ``services_script`` is detected from the script merely
    *existing* on disk, so it would activate a line a repo deliberately left
    commented out.  Reading them off the file — via the same extractor C002
    uses to decide the file drifted — keeps every per-repo choice and makes
    the written bytes exactly the canonical the checker compares against.

    That only holds for values the extractor knows about, though, which is what
    made this the FND-604 defect: a value it did not read back was deleted on
    every resync with nothing failing until CI reddened in a later tier. The
    ``unpreserved=`` guard closes the general case — a file declaring anything
    the canonical has no place for is refused rather than rewritten.

    Returns the ``(path, status)`` pairs this call wrote or left alone, for
    ``main()``'s ``--json`` touched-files manifest.
    """
    tests_dest = root / ".github" / "workflows" / "tests.yaml"
    if not tests_dest.exists():
        tests_dest.parent.mkdir(parents=True, exist_ok=True)
        tests_dest.write_text(render("tests.yaml", **kwargs), encoding="utf-8")
        print(f"scaffolded: {tests_dest}")
        return [(tests_dest, "scaffolded")]
    if resync:
        return _resync_scaffold(
            tests_dest,
            "tests.yaml",
            _tests_yaml_resync_params,
            unpreserved=unpreserved_tests_yaml_declarations,
        )
    print(
        f"ok (exists): {tests_dest}"
        "  (edit freely; C002 tracks drift at WARN — pass"
        " --resync to re-render from the canonical)"
    )
    return [(tests_dest, "exists")]


def _tests_yaml_resync_params(text: str) -> dict[str, str] | None:
    """Params to re-render *text* (a tests.yaml) with, or ``None`` to skip.

    ``app_name`` is identity-defining: it names the app in every downstream CI
    input and derives ``app-image-name``.  If it can't be read back, ``render``
    would fall back to ``"app"`` and the resync would quietly rename the app's
    own workflow inputs while reporting success.  Skip rather than guess — a
    file that drifted past its own name needs a human, not a re-render.
    """
    params = extract_tests_yaml_params(text)
    return params if params.get("app_name") else None


def _renovate_json_resync_params(text: str) -> dict[str, str] | None:
    """Params to re-render *text* (a renovate.json) with, or ``None`` to skip.

    ``extract_renovate_automerge`` answers ``"true"`` for anything it cannot
    parse.  That default is right for its other callers, but here it would
    turn an unparseable soft-mode file into the auto-merge canonical — a
    0-touch policy change (Renovate merging without a human) smuggled in under
    a structural catch-up.  Only resync when the mode is genuinely read.
    """
    try:
        json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return {"automerge": extract_renovate_automerge(text)}


def _resync_scaffold(
    dest: pathlib.Path,
    template_name: str,
    extract: Callable[[str], dict[str, str] | None],
    unpreserved: Callable[[str, str], list[str]] | None = None,
) -> list[tuple[pathlib.Path, str]]:
    """Re-render an existing write-if-absent scaffold from its canonical.

    One implementation for every ``--resync`` target: each differs only in
    which template it renders and how its per-repo values are read back, so
    the guards below — the identity check, the C002-identical comparison, the
    backup — cannot end up applied to one target and forgotten on another.

    *extract* returns the render params read off the on-disk content, or
    ``None`` when they can't be read confidently enough to rewrite the file.

    *unpreserved* names the declarations a re-render would delete, and turns a
    non-empty answer into a refusal (FND-604). It is per-target because "a
    declaration the canonical has no place for" is a YAML-shaped question:
    ``tests.yaml`` passes the key-set comparison, ``renovate.json`` passes
    nothing, since its own extractor already refuses anything it cannot parse
    and its canonical is the whole preset rather than a scaffold apps extend.

    Returns the ``(path, status)`` pairs for ``main()``'s ``--json`` manifest.
    """
    try:
        existing = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"skipped: {dest} is unreadable ({exc}); left untouched")
        return [(dest, "exists")]

    params = extract(existing)
    if params is None:
        print(
            f"skipped: {dest} has drifted too far to re-render safely"
            " (its per-repo values can't be read back); left untouched"
        )
        return [(dest, "exists")]

    target = render(template_name, **params)

    # Pin-insensitive, matching C002's own comparison: --resync must rewrite
    # exactly what C002 calls drift and nothing else. Comparing raw bytes
    # instead would rewrite (and back up) a file on every run whenever an
    # action pin differs from the template's — churn on a file the checker
    # already considers clean.
    if strip_action_pins(existing) == strip_action_pins(target):
        print(f"ok (up to date): {dest}")
        return [(dest, "unchanged")]

    # Refuse to downgrade. A re-render replaces the whole file, so anything the
    # canonical template has no place for is deleted — and the two that bit the
    # fleet hardest (an explicit `secrets:` mapping downgraded to `secrets:
    # inherit`, a dropped `force-external-runtime: true`) failed nowhere near
    # here: CI went red later, in the integration and e2e legs, with a
    # credential error that read as a source-system problem (FND-604, recurring
    # FND-110's damage via a different cause). Both are now carried forward, so
    # reaching this branch means the file declares something else again —
    # refuse, name it, and let a human decide, rather than backing it up and
    # trusting someone to diff the .bak. Ordered after the identity check so a
    # file that is already canonical still reports "up to date" rather than a
    # refusal it does not need.
    dropped = unpreserved(existing, target) if unpreserved is not None else []
    if dropped:
        print(
            f"skipped: {dest} declares {format_dropped_declarations(dropped)}, which the"
            " canonical template has no place for — a re-render would delete"
            " them; left untouched. Reapply the structural update by hand, or"
            " remove those declarations first."
        )
        return [(dest, "exists")]

    # Unconditional backup, unlike the --enforce force-overwrite path below —
    # which skips it when the existing content is one of the two canonical
    # renders. There is no equivalent recognisable set here: the
    # overwhelmingly common case is a file that was pristine for an *older*
    # conformance version, and old templates aren't available to recognise it.
    # So assume the difference might be a hand edit and always leave it
    # recoverable. `*.bak` is in the bootstrap-managed .gitignore, so this
    # never lands in a commit, and GitHub only parses .yml/.yaml under
    # .github/workflows/ so a backed-up workflow can't register as a second
    # workflow either.
    bak = dest.with_suffix(dest.suffix + ".bak")
    bak.write_text(existing, encoding="utf-8")
    print(f"backed up: {bak}  (previous content; reapply any hand edits from it)")
    dest.write_text(target, encoding="utf-8")
    print(f"updated: {dest}")
    return [(bak, "backed_up"), (dest, "updated")]


def _sync_renovate_json(
    root: pathlib.Path, kwargs: dict[str, str], force_renovate: bool, resync: bool
) -> list[tuple[pathlib.Path, str]]:
    """renovate.json — write-if-absent normally; force-overwrite when
    ``--enforce`` or ``--renovate-automerge`` is passed explicitly so
    re-running with ``--enforce true`` (or ``--renovate-automerge true``)
    upgrades a soft-mode repo without needing to delete the file first.

    *force_renovate* and *resync* are different operations on the same file
    and are checked in that order: the former CHANGES the enforcement mode to
    what was asked for, the latter PRESERVES whatever mode the file already
    declares and fixes only its structure.  So an explicit mode flag wins —
    passing it alongside ``--resync`` is an intentional mode change, and
    having the resync's read-back-off-disk value quietly override it would
    make the explicit flag a no-op.

    Returns the ``(path, status)`` pairs this call wrote or left alone —
    possibly two entries (the ``.bak`` backup plus the updated file itself)
    when a customised ``renovate.json`` is force-overwritten or resynced.
    """
    renovate_dest = root / "renovate.json"
    if not renovate_dest.exists():
        renovate_dest.write_text(render("renovate.json", **kwargs), encoding="utf-8")
        print(f"scaffolded: {renovate_dest}")
        return [(renovate_dest, "scaffolded")]
    if force_renovate:
        existing = renovate_dest.read_text(encoding="utf-8")
        target = render("renovate.json", **kwargs)
        if existing == target:
            print(f"ok (up to date): {renovate_dest}")
            return [(renovate_dest, "unchanged")]
        results: list[tuple[pathlib.Path, str]] = []
        canonical_hard = render("renovate.json", automerge="true")
        canonical_soft = render("renovate.json", automerge="false")
        if existing not in (canonical_hard, canonical_soft):
            bak = renovate_dest.with_suffix(".json.bak")
            bak.write_text(existing, encoding="utf-8")
            print(f"backed up: {bak}  (had custom content; review before committing)")
            results.append((bak, "backed_up"))
        renovate_dest.write_text(target, encoding="utf-8")
        print(f"updated: {renovate_dest}")
        results.append((renovate_dest, "updated"))
        return results
    if resync:
        return _resync_scaffold(
            renovate_dest, "renovate.json", _renovate_json_resync_params
        )
    print(
        f"ok (exists): {renovate_dest}"
        "  (edit freely; pass --enforce or --renovate-automerge to update"
        " enforcement mode, or --resync to fix structural drift while keeping it)"
    )
    return [(renovate_dest, "exists")]


def _sync_ci_system_deps(
    root: pathlib.Path, system_deps: str
) -> list[tuple[pathlib.Path, str]]:
    """.github/ci-system-deps.txt — the apt packages CI installs before ``uv sync``.

    checks.yml renders the packages inline, but the conformance suite's D-series
    leg syncs the caller's resolved environment inside the *vendored*
    ``run-conformance-detect`` action, where no rendered per-repo value can
    reach it. That step reads this file instead, guarded by ``hashFiles`` so it
    no-ops in every repo that doesn't need it. Same value, two surfaces, one
    flag (``--system-deps``).

    Only written when there are packages to declare: an empty file would make
    ``hashFiles`` non-empty and turn the guarded step into a pointless
    ``apt-get update`` on every D-leg run.
    """
    dest = root / ".github" / "ci-system-deps.txt"
    if not system_deps:
        return []
    return [(dest, _bootstrap_file(dest, f"{system_deps}\n"))]


def _sync_gitignore(root: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """.gitignore — write-if-absent scaffold. C003 warns about missing entries."""
    gitignore_dest = root / ".gitignore"
    if not gitignore_dest.exists():
        gitignore_dest.write_text(render(".gitignore"), encoding="utf-8")
        print(f"scaffolded: {gitignore_dest}")
        return [(gitignore_dest, "scaffolded")]
    print(
        f"ok (exists): {gitignore_dest}  (edit freely; C003 warns on missing entries)"
    )
    return [(gitignore_dest, "exists")]


_CONNECTOR_REVIEW_BEGIN = "<!-- BEGIN APPLICATION SDK CONNECTOR REVIEW -->"
_CONNECTOR_REVIEW_END = "<!-- END APPLICATION SDK CONNECTOR REVIEW -->"


class ConnectorReviewMergeError(ValueError):
    """A connector-review config cannot be safely merged."""

    def __init__(self, dest: pathlib.Path, reason: str) -> None:
        super().__init__(f"cannot safely merge {dest}: {reason}")


_RETIRED_CONNECTOR_REVIEW_HOOK_COMMANDS = frozenset(
    {
        ".claude/hooks/check-review-before-commit.sh",
        ".claude/hooks/review-rules-freshness.sh",
    }
)
_CONNECTOR_REVIEW_REMINDER_COMMAND = ".claude/hooks/connector-review-reminder.sh"
_KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK = """\
## Mandatory pre-commit review (L1–L4)

Before ANY `git commit`, the current changes MUST pass the `connector-review`
skill: the L1 conformance suite plus every applicable L2/L3/L4 review rule.
A PreToolUse hook blocks unreviewed commits; editing after a review invalidates
it, so re-review after fixes.

- L2/L4 rules: fetched from `atlanhq/application-sdk@main` into
  `.mothership/.cache/review-rulesets/` by `scripts/fetch-review-rules.sh`.
- L3 rules: `.mothership/review-rulesets/connector-app/` (this repo).
- Never restate rule text in this file or in prompts — the rule files are the
  only authority. If a rule seems wrong, change it in its source repo.
- Local review is fast feedback. The PR-label CI review remains authoritative.
- Emergency bypass (humans only, discouraged): `SKIP_CONNECTOR_REVIEW=1`.
"""


def _is_retired_connector_review_hook(command: object) -> bool:
    """Match the old fixed hook paths, including project-directory prefixes."""
    return isinstance(command, str) and any(
        command == path or command.endswith(f"/{path}")
        for path in _RETIRED_CONNECTOR_REVIEW_HOOK_COMMANDS
    )


def _merge_connector_review_settings(dest: pathlib.Path) -> str | None:
    """Replace retired kit hooks with one non-blocking reminder."""
    if not dest.exists():
        settings: dict[str, object] = {}
        original = ""
    else:
        try:
            original = dest.read_text(encoding="utf-8")
            loaded = json.loads(original)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConnectorReviewMergeError(dest, str(error)) from error
        if not isinstance(loaded, dict):
            raise ConnectorReviewMergeError(dest, "root must be a JSON object")
        settings = loaded

    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
        settings["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ConnectorReviewMergeError(dest, "'hooks' must be a JSON object")
    changed = False
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise ConnectorReviewMergeError(dest, f"hooks.{event} must be an array")
        retained = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                retained.append(entry)
                continue
            entry_hooks = entry["hooks"]
            kept_hooks = [
                hook
                for hook in entry_hooks
                if not (
                    isinstance(hook, dict)
                    and _is_retired_connector_review_hook(hook.get("command"))
                )
            ]
            if len(kept_hooks) == len(entry_hooks):
                retained.append(entry)
            elif kept_hooks:
                retained.append({**entry, "hooks": kept_hooks})
                changed = True
            else:
                changed = True
        if retained != entries:
            hooks[event] = retained
    reminder_entries = hooks.setdefault("SessionStart", [])
    if not isinstance(reminder_entries, list):
        raise ConnectorReviewMergeError(dest, "hooks.SessionStart must be an array")
    if not any(
        isinstance(entry, dict)
        and any(
            isinstance(hook, dict)
            and hook.get("command") == _CONNECTOR_REVIEW_REMINDER_COMMAND
            for hook in entry.get("hooks", [])
            if isinstance(entry.get("hooks"), list)
        )
        for entry in reminder_entries
    ):
        reminder_entries.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _CONNECTOR_REVIEW_REMINDER_COMMAND,
                        "timeout": 10,
                    }
                ]
            }
        )
        changed = True
    return json.dumps(settings, indent=2) + "\n" if changed else original


def _merge_connector_review_claude(dest: pathlib.Path) -> str:
    """Return CLAUDE.md with one replaceable, centrally-owned review block."""
    block = render("connector-review-claude.md")
    if not dest.exists():
        return block
    try:
        text = dest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConnectorReviewMergeError(dest, str(error)) from error

    begin = text.find(_CONNECTOR_REVIEW_BEGIN)
    end = text.find(_CONNECTOR_REVIEW_END)
    if begin == -1 and end == -1:
        if "## Mandatory pre-commit review" in text:
            legacy_begin = text.index("## Mandatory pre-commit review")
            if (
                text[legacy_begin:].rstrip()
                != _KNOWN_LEGACY_CONNECTOR_REVIEW_BLOCK.rstrip()
            ):
                raise ConnectorReviewMergeError(
                    dest, "unmarked connector-review block; migrate it manually first"
                )
            return f"{text[:legacy_begin].rstrip()}\n\n{block}"
        return f"{text.rstrip()}\n\n{block}"
    if begin == -1 or end == -1 or end < begin:
        raise ConnectorReviewMergeError(dest, "malformed managed review block")
    end += len(_CONNECTOR_REVIEW_END)
    return f"{text[:begin]}{block.rstrip()}\n{text[end:].lstrip()}"


def _sync_connector_review_kit(
    root: pathlib.Path, settings: str | None, claude: str
) -> list[tuple[pathlib.Path, str]]:
    """Write the review kit after its merge targets were preflighted."""
    changes: list[tuple[pathlib.Path, str]] = []
    for dest_rel, template_name, executable in MANAGED_CONNECTOR_REVIEW_FILES:
        dest = root / dest_rel
        changes.append(
            (dest, _bootstrap_file(dest, render(template_name), executable=executable))
        )

    for dest_rel in RETIRED_CONNECTOR_REVIEW_FILES:
        dest = root / dest_rel
        if _retire_file(dest) == "removed":
            changes.append((dest, "removed"))

    if settings is not None:
        settings_dest = root / ".claude" / "settings.json"
        changes.append((settings_dest, _bootstrap_file(settings_dest, settings)))

    claude_dest = root / "CLAUDE.md"
    changes.append((claude_dest, _bootstrap_file(claude_dest, claude)))

    gitignore_dest = root / ".gitignore"
    ignore_line = ".mothership/.cache/"
    text = gitignore_dest.read_text(encoding="utf-8") if gitignore_dest.exists() else ""
    if ignore_line in text.splitlines():
        print(f"ok (exists): {gitignore_dest}  (connector-review cache ignored)")
        changes.append((gitignore_dest, "exists"))
    else:
        updated = f"{text.rstrip()}\n{ignore_line}\n" if text else f"{ignore_line}\n"
        changes.append((gitignore_dest, _bootstrap_file(gitignore_dest, updated)))
    return changes


def _sync_contract_ledger(root: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """contract_schema.lock.json — write-if-absent scaffold.

    B006 (StaleContractLedger) is a hard FAIL-tier rule active from day one:
    with no ledger present, the ledger-absent fallback loads the SDK's own
    bundled ledger, which has none of the app's fields recorded, so any app
    with existing entrypoint contract fields fails enforced mode on its very
    first run. Seed the baseline from current source — same output as
    running ``gen-contract-ledger`` by hand.

    "Same output as gen-contract-ledger" is literal, and that is why the
    baseline comes from ``load_ledger_baseline``: the app's fields are
    discovered from its own source, and the SDK's packaged template contracts
    are never copied in. ``build_ledger`` is append-only, so a seeded entry
    could never be removed afterwards — see the helper's docstring.
    """
    ledger_dest = root / "contract_schema.lock.json"
    if not ledger_dest.exists():
        from conformance.suite.checks.deprecation._ledger_schema import (
            load_ledger_baseline,
            serialize,
        )
        from conformance.tools.generate_contract_ledger import build_ledger

        ledger = build_ledger(root, load_ledger_baseline(ledger_dest))
        ledger_dest.write_text(serialize(ledger), encoding="utf-8")
        print(f"scaffolded: {ledger_dest} ({len(ledger.fields)} fields)")
        return [(ledger_dest, "scaffolded")]
    print(
        f"ok (exists): {ledger_dest}"
        "  (run `gen-contract-ledger` to refresh; B005/B006 track drift)"
    )
    return [(ledger_dest, "exists")]


def main(argv: list[str]) -> int:
    """Write the SKILL.md shim and standard CI workflows into the current repo."""
    if "-h" in argv or "--help" in argv:
        print(BOOTSTRAP_USAGE)
        return 0

    # --json is a plain output-mode toggle (like -h/--help), not a --flag
    # value pair, so it's stripped before parse_bootstrap_args ever sees it
    # rather than being added to args.FLAGS.
    emit_json = "--json" in argv
    if emit_json:
        argv = [a for a in argv if a != "--json"]

    kwargs = parse_bootstrap_args(argv)
    root = pathlib.Path.cwd()

    # bootstrap scaffolds a *consumer app* repo. Every file it would write —
    # SKILL.md, the managed workflow/action shims, tests.yaml, renovate.json,
    # .gitignore, contract_schema.lock.json — is either hand-maintained here
    # or simply doesn't apply to a library repo. No-op the entire write phase
    # rather than special-casing each managed file individually — a per-file
    # guard silently stops covering new managed files the moment one is added
    # without updating it (this replaced an earlier guard that covered only
    # SKILL.md and missed MANAGED_WORKFLOWS/MANAGED_ACTION_FILES, which are
    # just as hand-authored in this repo).
    if _is_inside_conformance_repo(root):
        print(
            "skipped: bootstrap is a no-op inside the"
            " atlan-application-sdk-conformance repo itself"
            " (its .github/, SKILL.md, tests.yaml, and renovate.json are"
            " hand-maintained, not bootstrap-managed)"
        )
        if emit_json:
            print(json.dumps({"skipped": True, "touched": [], "unchanged": []}))
        return 0

    # force_renovate must reflect only an *explicit* flag on this invocation,
    # captured before autodetection fills kwargs["enforce"] in from an existing
    # conformance.yaml -- renovate.json stays write-if-absent on a bare re-run
    # even though conformance.yaml's enforcement mode is now auto-detected.
    # --renovate-automerge counts too: it is the lever that governs exactly
    # this file, so passing it and having the file left alone would be a no-op
    # flag.
    force_renovate = bool(kwargs["enforce"] or kwargs["renovate_automerge"])
    # Popped before autodetection and the render-kwargs derivation below: it is
    # a write-mode toggle, not a template variable, and render() takes an exact
    # keyword set.
    resync = kwargs.pop("resync") == "true"
    # --resync is "pull forward everything bootstrap owns that a bare re-run
    # leaves alone", and the review kit is exactly that: centrally owned, and
    # merged rather than clobbered, so it belongs to the same catch-up. The
    # flag stays meaningful on its own for a repo adopting the kit without
    # resyncing its scaffolds, and which of the two happened decides how an
    # unmergeable target is reported below.
    kit_requested = kwargs.pop("connector_review_kit") == "true"
    connector_review_kit = kit_requested or resync
    apply_bootstrap_autodetection(kwargs, root)

    # Validate shared config before bootstrap writes anything. The kit must
    # never overwrite a hand-maintained review block or a malformed settings
    # file during a fleet rollout.
    connector_review_settings: str | None = None
    connector_review_claude = ""
    if connector_review_kit:
        try:
            connector_review_settings = _merge_connector_review_settings(
                root / ".claude" / "settings.json"
            )
            connector_review_claude = _merge_connector_review_claude(root / "CLAUDE.md")
        except ConnectorReviewMergeError as error:
            # Asked for by name: the whole invocation was about the kit, so
            # fail loudly and write nothing.
            if kit_requested:
                print(f"error: {error}", file=sys.stderr)
                return 2
            # Implied by --resync: the kit is one target among several
            # independent ones, so skip it the way a scaffold whose values
            # can't be read back is skipped (see _resync_scaffold) and let the
            # rest of the resync land. Otherwise one hand-edited CLAUDE.md
            # would block a repo's tests.yaml/renovate.json catch-up too.
            print(
                f"skipped: {error}; the connector review kit was left untouched"
                " (migrate the block by hand, then re-run with"
                " --connector-review-kit)"
            )
            connector_review_kit = False

    # Resolve the two 0-touch levers, each independently expressible:
    #
    #   --conformance-blocking → exit_zero  (conformance.yaml)
    #   --renovate-automerge   → automerge  (renovate.json)
    #
    # --enforce is the shorthand that sets both at once, so an explicit
    # granular flag wins over it and an omitted one inherits it. enforce
    # itself is explicit-or-detected here (autodetection ran above);
    # enforce="" (never set, nothing to detect) → hard defaults.
    #
    # Neither lever, and not --enforce either, touches the tests gate: whether
    # `tests / Tests Gate` is a required, unbypassable check is a GitHub
    # branch-protection setting with no prerequisite here, deliberately kept
    # out of this derivation so the gate never waits on the 0-touch bar
    # (FND-347).
    enforce = kwargs.pop("enforce")
    shorthand = "false" if enforce == "false" else "true"
    conformance_blocking = kwargs.pop("conformance_blocking") or shorthand
    renovate_automerge = kwargs.pop("renovate_automerge") or shorthand
    kwargs["exit_zero"] = "false" if conformance_blocking == "true" else "true"
    kwargs["automerge"] = renovate_automerge

    # Structured record of every path this invocation touched or left
    # unchanged, for --json below. Populated alongside (never instead of) the
    # human-readable prints above and in the sync helpers, so a caller that
    # needs touched_files for a revert-scope decision (see
    # remediate-finding.prose.md) doesn't have to re-derive it by pattern
    # matching this command's prose stdout. Reported repo-relative to match
    # touched_files' documented convention (remediate-finding.prose.md) and
    # the K003/K004 `git status --porcelain`-derived case, which are both
    # naturally repo-relative already.
    touched: list[str] = []
    unchanged: list[str] = []

    def _record(path: pathlib.Path, status: str) -> None:
        rel = str(path.relative_to(root))
        (touched if status in _TOUCHED_STATUSES else unchanged).append(rel)

    skill_md = root / ".claude" / "skills" / "remediate" / "SKILL.md"
    _record(skill_md, _bootstrap_file(skill_md, render("remediate.md", **kwargs)))
    for name in MANAGED_WORKFLOWS:
        dest = root / ".github" / "workflows" / name
        _record(dest, _bootstrap_file(dest, render(name, **kwargs)))

    # Shims bootstrap once installed and now removes. See RETIRED_WORKFLOWS.
    # Only an actual deletion is recorded: a repo that never had the file (or
    # already dropped it) has no such path, and listing it under `unchanged`
    # would put a file that does not and will not exist into the manifest.
    for dest_rel in RETIRED_FILES:
        dest = root / dest_rel
        if _retire_file(dest) == "removed":
            _record(dest, "removed")

    # Non-workflow files referenced by conformance-reusable.yaml via a local
    # `./...`-relative path, which GitHub resolves against the caller's
    # checkout — every consumer repo needs its own copy or the C/D-series
    # (and any other series whose paths filter matches) legs fail with
    # "Can't find action.yml". Static, always-overwrite like MANAGED_WORKFLOWS.
    for dest_rel, template_name in MANAGED_ACTION_FILES:
        dest = root / dest_rel
        _record(dest, _bootstrap_file(dest, render(template_name)))

    for path, status in _sync_tests_yaml(root, kwargs, resync):
        _record(path, status)
    for path, status in _sync_renovate_json(root, kwargs, force_renovate, resync):
        _record(path, status)
    for path, status in _sync_ci_system_deps(root, kwargs["system_deps"]):
        _record(path, status)
    for path, status in _sync_gitignore(root):
        _record(path, status)
    for path, status in _sync_contract_ledger(root):
        _record(path, status)
    if connector_review_kit:
        for path, status in _sync_connector_review_kit(
            root, connector_review_settings, connector_review_claude
        ):
            _record(path, status)

    if emit_json:
        print(
            json.dumps({"skipped": False, "touched": touched, "unchanged": unchanged})
        )

    return 0
