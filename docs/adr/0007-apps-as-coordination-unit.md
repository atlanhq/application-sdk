# ADR-0007: Apps as the Unit of Inter-App Coordination

> **⚠️ Status: Under Review — [BLDX-878](https://linear.app/atlan-epd/issue/BLDX-878/clarify-inter-app-calls-in-sdk-v3)**
>
> Inter-app `call()` / `call_by_name()` is **deactivated** in the SDK pending resolution of the duplication with Automation-Engine DAG orchestration. The content below captures the original decision and may be revised.

## Status
**Under Review** — see BLDX-878

## Context

When one App needs to invoke another, we needed to decide the coordination mechanism. Temporal offers two options:
- **Child workflows**: The calling workflow starts another workflow as a child
- **Activities**: The calling workflow invokes a shared activity function

The choice affects durability, isolation, observability, and coupling between Apps.

## Decision

We chose **workflow-to-workflow coordination**: Apps call other Apps as Temporal child workflows. Tasks (`@task` methods) are strictly internal to an App and never callable from outside.

## Options Considered

### Option 1: Child Workflow Coordination (Chosen)

Apps invoke other Apps as child workflows via `app/client.py`:

```python
class Orchestrator(App):
    async def run(self, input: OrchestratorInput) -> OrchestratorOutput:
        # Import only the contracts — not the child's implementation
        from data_processor.contracts import ProcessorInput, ProcessorOutput

        result = await self.context.call_by_name(
            "data-processor",
            ProcessorInput(records=data),
            output_type=ProcessorOutput,
            task_queue="data-processor-queue",
        )
        return OrchestratorOutput(count=result.processed_count)
```

**Pros:**
- **Independent lifecycle**: Child workflows can retry and timeout independently
- **Observability**: Each App appears as a distinct workflow in Temporal UI with its own history
- **Durability**: Child workflow state persists even if parent fails temporarily
- **Loose coupling**: `call_by_name()` requires no code import — just the app name string and its contracts
- **Correlation**: Parent-child relationship tracked via `_parent_run_id` for distributed tracing
- **Version isolation**: Can run different versions of the same App simultaneously
- **Polyglot future**: Child workflows can be written in different languages since Temporal is language-agnostic

**Cons:**
- **Overhead**: Child workflows have more overhead than activities
- **Type checking**: `call_by_name()` is string-based — mitigated by importing only contract dataclasses (not the child's implementation class)

### Option 2: Activity-Based Coordination (Not Chosen)

Expose App operations as shared activities that others can call.

**Cons:**
- **No independent lifecycle**: Activities can't have their own retries independent of the caller
- **No observability boundary**: Activities don't appear as separate workflows in UI
- **Tight coupling**: Must import the activity function
- **No versioning**: Can't run v1 and v2 of the same "app" simultaneously

### Option 3: Direct Imports Between Apps (Not Chosen)

Require Apps to directly import each other's App classes.

**Cons:**
- **Tight coupling**: Parent depends on child's exact class
- **Deployment coupling**: Can't deploy parent and child independently
- **Circular import risk**: A→B→A dependencies
- **Version coupling**: Parent must use the exact child version it imports

## Rationale

1. **Apps as units of execution**: The unit of execution of an app translates to a Temporal workflow. Child workflows preserve this — each App is a complete, observable workflow.
2. **Continue-as-new support**: Long-running Apps can use continue-as-new to reset history without breaking callers. This only works with child workflows, not activities.
3. **Independent observability**: Each App appears in Temporal UI as a distinct workflow. You can see parent→child relationships and debug independently.
4. **Loose coupling via `call_by_name()`**: Apps in different packages/repositories can coordinate without code imports. The parent defines local contract copies; the child evolves independently.
5. **Tasks are private**: `@task` methods are implementation details. External callers invoke the App (workflow), not individual activities. This enforces encapsulation.

## Consequences

**Positive:**
- Clear boundary: App = Workflow, Task = Activity (internal)
- Independent lifecycles, retries, and observability per App
- Loose coupling possible via `call_by_name()` with local contracts
- Full distributed tracing via correlation IDs
- Polyglot-ready: child workflows can be any Temporal-supported language

**Negative:**
- Child workflow overhead higher than activities
- Must handle child workflow failure modes
- `call_by_name()` is string-based — import child's contract dataclasses to preserve type safety

## Implementation

Child workflow invocation lives in `application_sdk/app/client.py`. The `App` exposes it through `self.context` inside `run()`. Import only the target app's contract dataclasses — not the app class itself — to maintain type safety while keeping deployments independent:

```python
# Import ONLY the contracts from the target app's package
from loader.contracts import LoaderInput, LoaderOutput

class MyApp(App):
    async def run(self, input: MyInput) -> MyOutput:
        result = await self.context.call_by_name(
            "atlan-loader",
            LoaderInput(records=input.records),
            output_type=LoaderOutput,
            task_queue="atlan-loader-queue",
        )
        return MyOutput(loaded=result.count)
```
