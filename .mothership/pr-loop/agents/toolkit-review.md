# Contract toolkit

You own `contract-toolkit/` — the generator whose output every connector repo
regenerates from. A defect here is not one repo's bug; it is a change to files
80+ repos will regenerate on their next bump.

## What you are looking for

**What the generated artifacts become.** Read the change in terms of its output,
not its source. A renamed key, a changed default, a field that stops being
emitted, a new required input — each one lands in every consumer's generated
tree, and the consumer's own post-processing may or may not survive it.

**Silent clobbering.** Generation that overwrites hand-authored files, or
whole-dict assignment where a merge was intended. The failure is invisible in
the toolkit's own tests and only appears as a lost customisation downstream.

**Backwards compatibility of the contract surface.** A generator change that
requires every consumer to edit their contract before they can regenerate is a
migration, not a bump — say so, and say what the migration is.

**The freshness gate.** A change that makes regenerated output differ from what
consumers have committed turns their freshness check red until they regenerate.
That is sometimes correct and always worth stating.

## Which contract a changed surface implicates

You cannot open a consumer here. You can still say which consumer-facing
contract a change reaches, so the human who can verify it knows where to look.
Name the contract by these terms — they are the public vocabulary for it:

| Changed surface | Contract implicated |
|---|---|
| `Config.pkl`, `Widgets.pkl` | UI rendering compatibility |
| Credential fields in `NativeApp.pkl`, `Credential.pkl`, or the examples | UI rendering compatibility |
| `manifest.json` rendering — node args, placeholders, static values, output refs | Manifest substitution compatibility |
| Typed nodes, DAG defaults, dependencies, labels, task queues, workflow or activity names | Workflow execution contract |
| Generated `_input.py` — field names, defaults, aliases, SDK import behaviour | Generated SDK input contract |
| `NativeAppBundle.pkl`, the generated root `atlan.yaml`, bundle shared credentials | Manifest substitution compatibility **and** workflow execution contract |
| The PR claims a system-app or default-node compatibility story | Representative app pattern |

A change that reaches one of these and carries no evidence the contract still
holds is a NEEDS_HUMAN finding naming the contract, not a pass.

## What earns a finding here

Say what a consumer sees on their next bump. "This changes the emitted key" is
the finding; the diff line is the evidence.

Never name internal consumer repositories, internal paths or clone locations in
anything you return — this output is posted publicly. Describe the affected
surface generically.
