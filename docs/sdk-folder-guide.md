# SDK Folders -- Building a House

You're building a house. Not from scratch -- you hired a construction company (the SDK). They show up with a full crew, tools, trucks, and a project plan. You just tell them what kind of house you want.

The house's job? Go collect metadata from databases and APIs, and deliver it to Atlan.

---

**`app/`** -- **The Architect.**
Draws the blueprint. Decides what gets built in what order: first the foundation, then walls, then roof. Your app extends this -- you define the plan, the SDK executes it.

**`execution/`** -- **The Project Manager.**
Follows the architect's blueprint step by step. If it rains and work stops at step 3, he doesn't restart from step 1 -- he picks up right where things left off. (This is Temporal under the hood.)

- `_temporal/interceptors/` -- Safety inspectors walking the site, checking every step without slowing things down.

**`handler/`** -- **The Front Door.**
Before construction starts, the homeowner (Atlan) knocks and asks: "Is the site ready? Can you reach the materials? Is the permit valid?" The front door answers these questions over HTTP.

**`clients/`** -- **The Trucks.**
Each truck drives to a different supplier to pick up materials. One truck goes to the SQL warehouse. Another goes to the REST API depot. Another to Azure's yard. Each truck has the right keys to get past the gate.

- `auth_strategies/` -- The key ring. Different gates need different keys: password, swipe card, fingerprint, OAuth handshake.
- `azure/` -- GPS coordinates for Microsoft Azure's supply yard.

**`contracts/`** -- **The Work Order.**
A standard form every job uses. "What do you need? (Input) What did you deliver? (Output)" Same format for every house, so anyone on the crew can read any work order.

**`transformers/`** -- **The Carpenter.**
Raw lumber (database metadata) comes off the truck. The carpenter cuts, shapes, and fits it into the house's frame (Atlan's Atlas format). Without the carpenter, you just have a pile of wood.

- `atlas/` -- Shapes wood into Atlan's exact frame style.
- `query/` -- Specializes in shaping SQL query lumber.
- `common/` -- Shared tools any carpenter can use.

**`io/`** -- **The Saw and the Drill.**
Two basic power tools. The saw cuts JSON. The drill bores through Parquet. Simple, essential, used everywhere.

**`outputs/`** -- **The Loading Dock.**
Finished pieces come off the assembly line and stack up here, neat and counted, ready to ship to the homeowner.

**`services/`** -- **The Storage Shed, the Notebook, and the Lockbox.**
- `objectstore` -- The storage shed. Big stuff goes here: files, extracted data, Parquet bundles.
- `statestore` -- The foreman's notebook. "Framing done. Electrical next." Tracks progress so a restart doesn't lose work.
- `secretstore` -- The lockbox. Passwords and API keys. Bolted shut, only the foreman has the combination.

**`storage/`** -- **The Forklift.**
Services says WHAT to store and WHERE. Storage is the forklift that actually moves it -- upload, download, list, delete.

**`infrastructure/`** -- **The Foundation and Pipes.**
Concrete, plumbing, wiring buried underground. You never see it, but the house collapses without it.

- `_dapr/` -- Standard plumbing (Dapr). Connects storage, secrets, and messaging through one system.
- `_redis/` -- A direct pipe to Redis when you need speed over abstraction.

**`server/`** -- **The Site Office.**
- `fastapi/` -- The main office window. Visitors (HTTP requests) come here.
- `mcp/` -- A side window where AI assistants can ask questions.
- `middleware/` -- The sign-in sheet at the door. Logs every visitor, times every meeting.

**`observability/`** -- **The Security Cameras.**
Cameras on every corner. Logs = footage of what happened. Metrics = gauges on the dashboard (how fast, how many). Traces = GPS trail of a single request from start to finish. When something breaks, you rewind the tape.

**`credentials/`** -- **The Badge Maker.**
Prints ID badges for the crew. API key badge, OAuth badge, certificate badge. Each supplier gate needs the right one.

**`common/`** -- **The Shared Toolbox.**
Hammer, tape measure, level. Basic tools every crew member borrows.

- `incremental/` -- Instead of demolishing and rebuilding the whole house, just fix the rooms that changed since last time.

**`decorators/`** -- **The Labels.**
Stick a label on a tool: "fire-resistant," "retry 3 times," "measure how long this takes." Adds behavior without modifying the tool itself.

**`templates/`** -- **Pre-built Room Kits.**
Need a standard bathroom? Here's the kit -- toilet, sink, pipes, tiles, instructions. Just install it. Same idea for common app patterns like SQL extraction.

**`docgen/`** -- **The Blueprint Printer.**
Reads the finished house and auto-generates a blueprint document. You don't draw it -- it draws itself from the code.

**`test_utils/`** -- **The Model House.**
A miniature version with fake walls and foam furniture. Test everything without risking the real build.

**`testing/`** -- **The Building Inspection.**
Deploys the real house in a real neighborhood (Kubernetes) and checks if the plumbing works, doors open, and roof doesn't leak.

- `scale_data_generator/` -- Simulates 10,000 people moving in at once to stress-test the house.

---

## Quick Reference

```
app/              Architect (the blueprint, your app's brain)
execution/        Project Manager (runs steps in order, survives crashes)
handler/          Front Door (answers Atlan's questions over HTTP)
clients/          Trucks (drive to databases/APIs, pick up data)
  auth_strategies/  Key ring (passwords, OAuth, tokens)
  azure/            GPS for Azure
contracts/        Work Order (standard input/output forms)
transformers/     Carpenter (raw data -> Atlan format)
  atlas/            Shapes into Atlas format
  query/            Shapes SQL query data
io/               Saw + Drill (read/write JSON and Parquet)
outputs/          Loading Dock (finished results, ready to ship)
services/         Shed + Notebook + Lockbox (files, state, secrets)
storage/          Forklift (moves files in/out of storage)
infrastructure/   Foundation + Pipes (Dapr, Redis, invisible but essential)
server/           Site Office (HTTP server, MCP, middleware)
observability/    Security Cameras (logs, metrics, traces)
credentials/      Badge Maker (prints auth credentials)
common/           Shared Toolbox (utilities everyone uses)
  incremental/      Only rebuild what changed
decorators/       Labels (add behavior without changing code)
templates/        Pre-built Room Kits (ready-made app patterns)
docgen/           Blueprint Printer (auto-generates docs)
test_utils/       Model House (mocks and fakes for testing)
testing/          Building Inspection (real end-to-end tests)
```

---

*The SDK is a construction crew. You draw the house -- they build it.*
