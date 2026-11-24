---
id: FP16
---
# A change that undoes a recent change says why

Principle: Undoing a recent change is fine when the PR says so and says why. A PR must not quietly
take back a recent fix by restoring lines it removed, removing lines it added, or reverting it.
Why: A recent fix usually answers a production problem. When it is undone without a word, often by
a merge, a refactor or a second fix for a different symptom, the first problem comes back and
nobody knows it was ever fixed.
Ask:
- For each history fact you are given, does the PR title, description or a code comment acknowledge the earlier change and give a reason for undoing it?
- Does the reason deal with the problem the earlier change fixed (solved another way, no longer applies, the fix was wrong), or does it only describe the new change?
- Where a function has changed three or more times in 30 days: is this another patch on the same symptom, and does the PR say what the earlier attempts missed?
Don't flag: anything with no history fact behind it. The facts are computed for you from git (lines
restored that a recent commit removed, lines removed that a recent commit added, a recent PR
reverted, a function changed three or more times in 30 days); do not look for reverts yourself. Do
not flag a revert that states its reason, generated files, lock files or formatting-only changes.
No conformance rule or CI gate checks this.
Severity: High when a recent fix for data loss, a failed run or a security problem is undone with
no reason given. Medium for any other unexplained undo or repeated change.
