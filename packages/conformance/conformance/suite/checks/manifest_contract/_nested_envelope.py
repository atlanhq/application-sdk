"""K020 ManifestArgsLegacyNestedEnvelope — check implementation.

The ``extract`` node of a committed ``app/generated/**/manifest.json`` still
emits the legacy ``args.metadata{}`` envelope instead of flat top-level args.

**Flat is the target shape.** contract-toolkit's ``flatManifestArgs`` defaults to
``true`` (``NativeApp.pkl``: *"Set to `false` only for legacy apps that
intentionally consume `args.metadata`"*), ``ExtractionInput`` declares the
standard config set flat, and the SDK's ``_normalize_ae_payload`` canonicalises a
nested payload *up* into those flat fields. Every layer converges on flat; the
nested envelope is the one hold-out.

Two causes, distinguishable from the repo, with different fixes:

``opt-out``
    ``contract/app.pkl`` sets ``flatManifestArgs = false``. A deliberate legacy
    choice. Fix: remove the override (or set it ``true``), move whatever the app
    reads out of ``args.metadata`` onto declared flat fields, and regenerate.
    An app pairing it with ``manifestMetadataArgs`` is pinning specific keys into
    the envelope and must unpick that too.

``stale-artifact``
    The contract does **not** set the flag, so it would render flat today — but
    the committed manifest is nested, i.e. it was generated before the flattening
    and never regenerated. Measured 2026-09-01: two apps are in this state on
    toolkit 0.19.2, while a third app on the *same* toolkit with the same unset
    flag renders flat — which is what proves the default already flattens and the
    nested artifact is stale rather than intended.

**Why this is worth a rule rather than a cleanup ticket.** A stale nested
manifest is a loaded gun. The published workflow DAG carries the nested shape,
so the next regeneration — or a platform re-render that fetches a fresh
manifest — flips nested → flat, and that transition is exactly where
``build_allparams_flat`` drops config: it recovers values by matching template
*paths*, so a key that moved from ``args.metadata.x`` to ``args.x`` resolves to
nothing and is stripped from the published DAG. The migration is the right thing
to do and it is also the moment of maximum risk, which is why the finding says
to verify published workflows rather than just flip the flag.

Anchored on ``contract/app.pkl`` — the manifest is a ``pkl eval`` output and,
being JSON, carries no comment syntax to suppress on (same reasoning as K013).
"""

from __future__ import annotations

import re
from pathlib import Path

from conformance.suite.checks.entrypoint_alignment._contract_entrypoints import (
    scan_contract as scan_contract_entrypoints,
)
from conformance.suite.schema.findings import Finding

from ..legacy_contract._directives_pkl import (
    _make_pkl_finding_suppressed,
    _parse_pkl_directives,
)
from ._manifest_args import collect_arg_keys
from ._manifest_refs import manifest_paths_for_contract

_RULE_ID = "K020"

_CONTRACT_PKL = "contract/app.pkl"

_FLAT_FLAG_RE = re.compile(r"^\s*flatManifestArgs\s*=\s*(?P<value>\w+)", re.MULTILINE)

# Pkl accepts both `manifestMetadataArgs = new Mapping {...}` and the block
# amend form `manifestMetadataArgs { ... }`; the fleet uses the former.
_METADATA_ARGS_RE = re.compile(r"^\s*manifestMetadataArgs\s*[={]", re.MULTILINE)


def _flat_flag(source: str) -> tuple[str | None, int]:
    """Return ``(value, 1-based line)`` for ``flatManifestArgs``; ``(None, 1)`` if unset."""
    m = _FLAT_FLAG_RE.search(source)
    if m is None:
        return None, 1
    return m.group("value"), source.count("\n", 0, m.start()) + 1


def scan_all(paths: list[Path], root: Path) -> list[Finding]:  # noqa: ARG001
    """Report each entrypoint whose committed manifest still nests args.

    No-ops when ``contract/app.pkl`` is absent, the P016 contract scan finds no
    entrypoints, or no manifest is readable — conservative, matching the
    package's WARN posture.
    """
    pkl_path = root / _CONTRACT_PKL
    try:
        source = pkl_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    contract = scan_contract_entrypoints(root)
    if contract.mode == "absent":
        return []

    flag_value, flag_line = _flat_flag(source)
    opted_out = flag_value == "false"
    pins_metadata_args = _METADATA_ARGS_RE.search(source) is not None
    directives = _parse_pkl_directives(source)

    findings: list[Finding] = []

    for path in manifest_paths_for_contract(root, contract):
        args = collect_arg_keys(path, root)
        if args is None:
            continue
        nested = sorted(args.nested_keys())
        if not nested:
            continue

        entry = Path(args.manifest_path).parent.name or "."
        shown = ", ".join(nested[:5]) + (", …" if len(nested) > 5 else "")

        if opted_out:
            cause = (
                "contract/app.pkl sets 'flatManifestArgs = false', so the toolkit "
                "renders the legacy envelope on purpose. Remove the override (the "
                "default is true), move whatever the app reads out of "
                "args.metadata onto declared flat fields on its Input contract, "
                "and regenerate with `pkl eval -m . contract/app.pkl`."
            )
            if pins_metadata_args:
                cause += (
                    " This contract also sets 'manifestMetadataArgs', which pins "
                    "specific keys into the envelope — unpick that in the same "
                    "change."
                )
        else:
            cause = (
                "contract/app.pkl does not set 'flatManifestArgs', so it would "
                "render flat today — this committed manifest predates the "
                "flattening and was never regenerated. Regenerate with "
                "`pkl eval -m . contract/app.pkl`."
            )

        findings.append(
            _finding(
                entry=entry,
                nested_count=len(nested),
                shown=shown,
                cause=cause,
                line=flag_line,
                directives=directives,
            )
        )

    return findings


def _finding(
    *,
    entry: str,
    nested_count: int,
    shown: str,
    cause: str,
    line: int,
    directives: dict,
) -> Finding:
    suppressed, justification = _make_pkl_finding_suppressed(
        rule_id=_RULE_ID, line=line, directives=directives
    )
    return Finding(
        rule_id=_RULE_ID,
        file=_CONTRACT_PKL,
        line=line,
        column=1,
        message=(
            f"Entrypoint '{entry}' still sends {nested_count} arg(s) inside the "
            f"legacy 'args.metadata' envelope ({shown}). Flat top-level args are "
            "the contract: ExtractionInput declares the standard config set flat "
            "and the SDK normalises nested payloads up into those fields, so the "
            f"envelope is the only layer still nested. {cause} "
            "Then declare the migrated keys on the entrypoint's Input contract so "
            "K018 covers them. "
            "IMPORTANT — the regeneration is also the risky moment: published "
            "workflow versions carry the nested shape, and the platform re-render "
            "recovers config by matching template paths, so keys moving from "
            "args.metadata.x to args.x resolve to nothing and are stripped "
            "(CONNECT-1318 / APPPLAT-371). Verify published workflows still carry "
            "their filters after the first re-render rather than assuming. "
            f"Suppress with '// conformance: ignore[{_RULE_ID}] <reason>'."
        ),
        suppressed=suppressed,
        suppression_justification=justification,
        discriminator=entry,
    )
