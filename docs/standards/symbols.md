# Symbols: Removal and Rename Process

The counterpart to [`env-vars.md`](env-vars.md), for the same reason and with a
sharper failure mode.

When you remove or rename an env var the SDK reads, a deployment still setting
the old name silently no-ops — bad, but the process survives. When you remove or
rename a **symbol**, every app that imports it stops importing at all. There is
no degraded mode: `ImportError` at collection, CI red across the fleet, on the
next routine lock refresh, with no warning anyone could have read beforehand.

That is not hypothetical. SDK 3.36.0 shipped
[#3685](https://github.com/atlanhq/application-sdk/pull/3685), which reshaped
`application_sdk/execution/_temporal/preflight_gate.py`. Nine names went away
with no deprecated aliases and no removal-version notice, and fifteen connector
repos stopped collecting tests (FND-2388).

The interesting part of that incident is that #3685 followed the deprecation
discipline everywhere *except* module-level Python symbols. It kept
`PREFLIGHT_GATE_MODE_ENV` as an import shim. It added `ATLAN_PREFLIGHT_GATE_MODE`
to `_REMOVED_ENV_VARS` exactly as `env-vars.md` prescribes. It gave the
behaviour change a `DEPRECATED_FAIL_OPEN_REMOVED_IN = "3.40.0"` horizon. It even
added a `__deprecated_members__` entry to `PreflightStatus`. So this was not
ignorance of the rule — there was a written, enforced process for removing an
**env var** and no equivalent for removing a **symbol**. This document is that
equivalent.

---

## The rule

> **You may not remove a name that was not already marked deprecated in the
> previous release.**

That is the whole policy, and it is enforced mechanically by
`.github/scripts/check_symbol_removals.py` (the **Symbol Removal Check**
workflow, a required status check), which compares the importable surface at
`HEAD` against the surface at the last stable `v*` tag.

Note what the rule does *not* say. It does not say "do not remove things" — the
SDK has to be able to shed surface. It says removal is a **two-step** operation
with a release boundary in the middle, exactly like every env var removal already
is.

Note also what you do **not** have to maintain: there is no registry of excused
removals, no allowlist, no annotation file. Turning a name into a deprecated
alias keeps the name *present in the tree*, and the gate is satisfied by its
presence. The excuse cannot drift away from the code, because the excuse **is**
the code.

### What the gate does not cover

It guards the **shape** of the surface, not its semantics. It does not compare
default *values* — changing `mode: PreflightGateMode = PreflightGateMode.SOFT`
to `mode: PreflightGateMode | None = None` produces no finding, because the
parameter still exists and still has a default. Nor does it see a changed return
type, a narrowed exception contract, or any behavioural change behind an
unchanged signature.

That is deliberate: the SDK changes defaults often and on purpose, and a gate
that argued about every one would be noise nobody reads. But treat it as a blind
spot, not as coverage. A default-value change alters behaviour for every caller
who never passed the argument, and a green run means only that no name vanished
and no signature narrowed — never that the behaviour is unchanged.

---

## How to deprecate, by symbol kind

Three markers, because Python gives a decorator nothing to attach to in two of
the three cases. All three are machine-readable: the conformance suite's
`gen-deprecations` scan records them into `deprecated_symbols.json`, which ships
with the conformance package, which every app's CI already runs. So a correctly
marked deprecation turns into a fleet-wide **B001** nudge with zero per-app work
— carrying your own migration text.

### A function, method or class → `@deprecated`

```python
from typing_extensions import deprecated

@deprecated(
    "resolve_gate_attempts is deprecated; use gate_attempts, which returns "
    "(attempts, complaint) and leaves the logging to its caller — will be "
    "removed in v3.40.0."
)
def resolve_gate_attempts(raw: object) -> int:
    """Resolve an app's declared gate attempts.

    .. deprecated:: 3.37
        Use :func:`gate_attempts`. Will be removed in v3.40.0.
    """
    attempts, complaint = gate_attempts(raw)
    if complaint:
        logger.warning("preflight_gate_max_attempts: %s; using %d", complaint, attempts)
    return attempts
```

Keep the body working by delegating to the replacement. An alias that raises is
not an alias — it is the same break with a better error message, and it belongs
in a `feat!:` commit (see [Deliberate breaks](#deliberate-breaks)).

### An enum member → `__deprecated_members__`

A decorator cannot reach a member: it is an assignment in a class body.

```python
class DataframeType(str, Enum):
    __deprecated_members__ = {
        "daft": "DataframeType.daft is deprecated; use DataframeType.pandas — "
                "will be removed in v4.0.0.",
    }

    pandas = "pandas"
    daft = "daft"
```

The dunder name keeps `EnumMeta` from reading the mapping as a member. See
`application_sdk/common/types.py`.

### A module-level constant → `_DEPRECATED_CONSTANTS` + PEP 562 `__getattr__`

A constant can carry neither marker above, so the vehicle is a module
`__getattr__`. It fires on *access*, so an app that never touches the name pays
nothing, and the warning names the caller's own line rather than this module's
import.

```python
import warnings

_DEPRECATED_CONSTANTS: dict[str, str] = {
    "CLASSIFICATION_VERDICT": (
        "CLASSIFICATION_VERDICT is deprecated; use PreflightClassification.VERDICT, "
        "the enum member carrying the same wire value — will be removed in v3.40.0."
    ),
}


def _deprecated_constant_value(name: str) -> object:
    if name == "CLASSIFICATION_VERDICT":
        return PreflightClassification.VERDICT.value
    raise AssertionError(name)


def __getattr__(name: str) -> object:
    """Serve the removed constants once more, with a deprecation warning (PEP 562)."""
    notice = _DEPRECATED_CONSTANTS.get(name)
    if notice is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(notice, DeprecationWarning, stacklevel=2)
    return _deprecated_constant_value(name)
```

Three constraints make this readable by the tooling, and all three matter:

- The mapping must be named **`_DEPRECATED_CONSTANTS`** and be a dict literal at
  module level. Both the surface gate and `gen-deprecations` read its keys
  statically.
- The module must define **`__getattr__`**. The mapping alone serves nothing;
  recording its keys without the shim would tell apps a name is importable when
  accessing it raises `AttributeError`.
- Do **not** also re-export the old name at module scope. A real binding resolves
  before `__getattr__` ever runs, so the alias never fires and the consumer gets
  no migration signal at all.

A value may also be a tuple of parts (`(replacement, note)`), which the extractor
joins — that shape exists because
[#3843](https://github.com/atlanhq/application-sdk/pull/3843) wrote it that way,
with the shared sentence and the removal version living in the `__getattr__`
f-string. Both halves are glued back together before grading. Prefer the plain
notice string: it is what a B001 finding shows a human.

---

## Every notice names a replacement and a removal version

Not style — enforced. **B002** flags a notice missing either, and **B003** flags
one whose stated removal version the SDK has already passed. Pick a horizon far
enough out that a connector on a quarterly upgrade cadence actually sees the
warning; the preflight-gate aliases used `v3.40.0` against a `3.37` deprecation,
which is a reasonable default shape.

When the horizon arrives, delete the alias. The surface gate stays quiet, because
the base release it compares against had the name marked.

---

## Private symbols are not covered by any of this

An underscore-prefixed module or name carries **no** compatibility promise, and
nothing in this document applies to it. Rename and delete freely.

That is a decision, recorded on FND-2388, not an accident. The alternative —
treating whatever the fleet happens to import as public — would freeze
`application_sdk/execution/_temporal/` permanently because fifteen repos reached
into it, and would tax every internal refactor thereafter.

The boundary was always declared. The capability manifest generator skips
`_`-prefixed module paths by construction, which is why
`docs/agents/sdk-capabilities.md` has never listed a single `preflight_gate`
symbol. Python simply enforces none of it: a leading underscore is a convention
with no runtime meaning, and `from pkg._private import thing` works exactly as
well as any other import. So the enforcement is **B008
`PrivateSdkModuleImport`**, which runs in consumer apps and flags any import that
reaches into an `_`-prefixed SDK module or name — in `tests/` as much as in
`app/`, because all fifteen repos FND-2388 wedged broke in `tests/`.

The two halves are a pair, and the split is the design:

| | Public name | Private name |
|---|---|---|
| **SDK deletes it** | Surface gate **blocks** the PR | Surface gate **reports** it, never blocks |
| **App imports it** | Fine | **B008** reports it |

The SDK keeps the right to change its internals; apps get told, once, to stop
depending on them.

Where an app genuinely needs something only a private symbol provides, that is an
SDK gap worth raising — not routing around. Say so in the suppression:
`# conformance: ignore[B008] no public equivalent — tracked in <issue>`.

---

## Deliberate breaks

Sometimes an alias is genuinely impossible, or the symbol is so new that nobody
can have adopted it. Those are allowed. They just have to be *declared*, so the
release automation prices them correctly:

```
feat!: drop the legacy gate contract

BREAKING CHANGE: resolve_gate_attempts is removed; use gate_attempts.
```

`release-version-bump.yaml` reads conventional commits (`feat` → minor, `fix` →
patch, `!` / `BREAKING CHANGE` → major). #3685's subject was a plain
`fix(preflight):`, which is how a fleet-breaking change rode out on a minor bump
with no changelog breaking note. A declared break relaxes the surface gate's
blocking findings to advisory.

Declaring a break is a real decision with a real cost — a major bump gates every
consumer's upgrade. Prefer the alias.

---

## Checklist when removing or renaming a symbol

1. Add the deprecation marker for the symbol's kind (above), with a replacement
   and a removal version in the notice.
2. Keep the old name working by delegating to the replacement.
3. Regenerate the deprecated-symbol manifest so B001 fans it out:
   ```sh
   uv run atlan-application-sdk-conformance gen-deprecations
   ```
   (`packages/conformance/tests/test_deprecations_manifest.py` fails your PR
   until you do.)
4. Refresh the capability manifest if the symbol is public:
   ```sh
   uv run poe regen-capabilities
   ```
5. Confirm the surface gate is clean:
   ```sh
   python3 .github/scripts/check_symbol_removals.py check --repo .
   ```
6. Update any docs that reference the old name, and mention the deprecation in
   the PR description so it lands in the changelog.
7. When the removal version arrives, delete the alias and its manifest entry in
   one PR.

## Checklist when the gate fails your PR

Read the job summary — it lists every name, with the tier.

- **Blocking, and you meant to keep compatibility** → add the alias (step 1
  above). This is the normal outcome.
- **Blocking, and the break is deliberate** → declare it in the commit subject.
- **Advisory** → nothing is required. It is telling you an SDK private that some
  app may be importing has gone; B008 is what moves those apps off it.
- **You believe it is a false positive** → say so on the PR rather than working
  around it. The gate has no suppression mechanism on purpose; a surface gate
  you can silence per-line is a surface gate that gets silenced.
