# Contract Toolkit

The contract toolkit is the Pkl package used to author Atlan native app
contracts. It generates the workflow config, credential config, Automation
Engine manifest, and typed SDK input model that the Application SDK serves at
runtime.

The source now lives in this repository under `contract-toolkit/`, but it is
released separately from the Python SDK package. App repos should depend on the
versioned Pkl package, not on the SDK source tree.

```pkl
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@<LATEST_VERSION>"
  }
}
```

Use the generated outputs in the usual SDK layout:

```text
app/generated/
|-- _input.py
|-- {name}.json
|-- atlan-connectors-{name}.json
`-- manifest.json
```

For the complete authoring reference, examples, and validation commands, see
`contract-toolkit/README.md` and `contract-toolkit/docs/reference.md`.

For public app-author examples, start with `contract-toolkit/examples/minimal`
for the smallest possible contract, `contract-toolkit/examples/tree-filters` for
include/exclude filter tree pickers, and `contract-toolkit/examples/full` for
every overridable feature (JDBC URL auth, all pipeline steps, `extraNodes`). The
generic capability examples include `connection-ref`, `fanin`, and
`publish-controls`; the toolkit README lists them all.
App-specific wiring examples should live with sample apps or the owning app
repository; test-only scenarios live under `contract-toolkit/tests/fixtures`.

For a step-by-step walkthrough of writing and generating a contract, see
[PKL Contracts](pkl-contracts.md).
