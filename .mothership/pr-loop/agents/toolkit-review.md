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

## What earns a finding here

Say what a consumer sees on their next bump. "This changes the emitted key" is
the finding; the diff line is the evidence.

Never name internal consumer repositories, internal paths or clone locations in
anything you return — this output is posted publicly. Describe the affected
surface generically.
