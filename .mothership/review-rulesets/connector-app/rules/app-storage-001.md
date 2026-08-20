---
schema: 2
id: APP-STORAGE-001
level: L2
category: correctness
globs: []
severity: HIGH
provenance: atlan-databricks-app app-review.md (STATE/DATA, SDK v3 platform rules)
suppressible: false
---
# Cross-activity data goes through the object store

- MUST pass data between activities via the object store under the task's
  `output_path` — activities can land on different pods.
- MUST NOT read a local file another activity produced ("local-first" reads):
  a pod that also ran the producer holds a partial slice on disk.
- MUST delete local files after a successful upload — retained shards fill
  the pod filesystem and poison later local reads.
- MUST NOT use module-level mutable state to pass data between tasks.
- MUST NOT write outside the task's `output_path`.
