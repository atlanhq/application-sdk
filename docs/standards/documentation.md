# Documentation Updates

When modifying SDK code, update corresponding conceptual documentation under `docs/concepts/` to reflect changes.

**Module to Concept Doc Mapping:**
*   `application_sdk/app/**` -> `docs/concepts/apps.md`
*   `application_sdk/templates/**` -> `docs/concepts/tasks.md`
*   `application_sdk/execution/**` -> `docs/concepts/apps.md`
*   `application_sdk/execution/progress*.py`, `application_sdk/execution/heartbeat.py` (watchdog paths) -> `docs/concepts/progress-and-stalls.md`
*   `application_sdk/handler/**` -> `docs/concepts/handlers.md`
*   `application_sdk/server/**` -> `docs/concepts/server.md`
*   `application_sdk/clients/**` -> `docs/concepts/clients.md`
*   `application_sdk/errors/**` -> `docs/concepts/common.md` (Error Handling section) and `docs/standards/exceptions.md`
*   `application_sdk/common/**` or `application_sdk/constants.py` -> `docs/concepts/common.md`
*   `application_sdk/transformers/**` -> `docs/concepts/common.md`
*   `application_sdk/infrastructure/**` -> `docs/concepts/apps.md` (infrastructure abstraction section)
*   `application_sdk/contracts/**` -> `docs/concepts/entry-points.md` (contract types section)
*   `application_sdk/credentials/**` -> `docs/concepts/handlers.md` (credential resolution section)
*   `application_sdk/storage/**` -> `docs/concepts/apps.md` (built-in storage tasks section)
*   `application_sdk/observability/**` -> `docs/concepts/monitoring.md`

## Code examples use current APIs

A Python example in a Markdown file teaches whatever it imports, so an example
on a deprecated symbol lands every app that copies it on a base it must migrate
off again. `.github/scripts/check_docs_deprecated_imports.py` (pre-commit hook
`check-docs-deprecated-imports`, run on every PR by the required `Pre-commit`
check) parses each Python block in every tracked Markdown file and fails on a
use of a symbol in `packages/conformance/conformance/data/deprecated_symbols.json`.
It uses conformance's own B001 matcher, so it matches what B001 matches in an
app: an import resolves through re-exports (`from application_sdk.templates
import SqlMetadataExtractor`), an enum member (`PreflightStatus.PARTIAL`)
through its enum, and a deprecated method by name. Deprecated properties are
not matched, in docs or in apps. SDK imports carry from one block to the next
on a page, so a block that only subclasses a deprecated base is still checked
against the earlier block's import.

When the check fails, rewrite the example onto the replacement the message
names. A block that shows a deprecated API on purpose (a v2 "before" snippet, a
page about a deprecated framework, a target kept until its replacement exists)
says so on the line directly above its opening fence:

````markdown
<!-- legacy-example: v2 "before" snippet; the v3 block below replaces it -->
```python
from application_sdk.transformers.atlas import AtlasTransformer
```
````

The reason is required. A page that presents itself as the current way to
build an app carries no marker: if it needs one, the page needs rewriting.

The check sees only Python blocks; a deprecated name in prose or in a table is
on the author. Run it on its own with
`uv run --frozen python .github/scripts/check_docs_deprecated_imports.py [FILE.md ...]`.

## Site build (`mkdocs build --strict`)

CI builds the docs site with `mkdocs build --strict` on every PR that touches `docs/` or `mkdocs.yml` (the Docs area of the SDK Gate, `.github/workflows/docs.yaml`). The `validation:` block in `mkdocs.yml` makes each of these a failure:

- **A page not in `nav`.** Add every new page under `docs/` to `nav` in `mkdocs.yml`. Working notes that are not site pages go under an `exclude_docs` path instead.
- **A link to a file outside `docs/`.** The site serves `docs/` only, so `../../application_sdk/main.py` is a 404 there. Link repo source with a full URL: `https://github.com/atlanhq/application-sdk/blob/main/application_sdk/main.py`.
- **An anchor the target page lacks.** MkDocs and GitHub slug headings differently: a heading with `&` or `—` gets a double hyphen on GitHub and a single one in MkDocs. Avoid punctuation in headings you link to, so both renderers agree.

Run `uv run mkdocs build --strict` locally before pushing a docs change.

