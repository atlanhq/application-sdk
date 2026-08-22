---
schema: 2
id: APP-SECRETS-001
level: L2
category: security
globs: []
severity: HIGH
suppressible: false
---
# Credentials never reach logs or errors

- MUST NOT log connection URLs, `engine.url`, `connect_args`, headers, or
  `repr()` of any client/engine object — they embed credentials.
- MUST NOT put credential material or raw connection config into exception
  messages, activity outputs, or metrics labels.
- Reference secrets by store path or env var name, never by value.
