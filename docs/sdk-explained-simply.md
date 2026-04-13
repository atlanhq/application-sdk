# The Atlan Application SDK -- Explained Like You're 10 (with Robots!)

Imagine you run a **Robot Factory**. Your job is to build robots that go out into the world, collect information from different places (databases, cloud services, APIs), and bring it all back to your headquarters -- **Atlan**.

The **Application SDK** is your **Robot-Building Kit**. Instead of building every robot from scratch with nuts and bolts, this kit gives you pre-made parts so you can snap together a working robot in no time.

---

## The Big Picture

```
 YOU (developer)
  |
  | "I need a robot that talks to Snowflake!"
  v
+---------------------------------------------+
|          ROBOT-BUILDING KIT (the SDK)        |
|                                              |
|  [Brain] [Arms] [Walkie-Talkie] [Backpack]  |
|  [Eyes]  [Legs] [Clipboard]     [Toolbelt]  |
+---------------------------------------------+
  |
  v
 YOUR ROBOT (a connector / app)
  |
  | goes out, collects metadata, brings it home
  v
 ATLAN (headquarters)
```

---

## The Robot Parts

### 1. The Brain -- `App` (base class)

Every robot needs a brain. The `App` class is the main brain of your robot. It knows:
- What its name is
- What job it's supposed to do
- How to start up and shut down
- How to follow a step-by-step plan (a **workflow**)

Think of it like a to-do list the robot follows: "First check if you can connect. Then go fetch the data. Then bring it home."

### 2. The To-Do List -- `Workflows` and `Tasks`

A **workflow** is like writing instructions on a card for your robot:

> **Step 1:** Check the password works (preflight)
> **Step 2:** Go to the database and look around (extraction)
> **Step 3:** Pack up what you found (transformation)
> **Step 4:** Deliver it to headquarters (upload)
> **Step 5:** Clean up after yourself (cleanup)

Each step on the card is a **task** (also called an activity). If the robot trips on Step 3, it doesn't start over from Step 1 -- it picks up right where it fell. That's because **Temporal** (the task manager) keeps track of progress, like a teacher watching over the robot's checklist.

### 3. The Arms -- `Clients`

The arms are how the robot **reaches out and touches things**. Need to grab data from:
- A **SQL database** (Snowflake, Postgres)? Use the SQL arm.
- An **API** (REST, GraphQL)? Use the HTTP arm.
- **Azure Blob Storage**? There's an arm for that too.
- **Redis** cache? Yep, there's one.

Each arm knows how to shake hands (authenticate), grab stuff, and bring it back safely.

### 4. The Walkie-Talkie -- `Handler` (HTTP Server)

Before the robot goes on a mission, headquarters needs to talk to it:
- "Hey robot, can you even reach that database?" (**preflight check**)
- "What tables and schemas are over there?" (**metadata fetch**)
- "Is this password correct?" (**auth check**)

The **Handler** is the robot's walkie-talkie -- it listens for HTTP requests from Atlan and responds. It's powered by FastAPI under the hood.

### 5. The Backpack -- `Storage` (ObjectStore, StateStore, SecretStore)

The robot needs places to put things:
- **ObjectStore** -- A big backpack where the robot stores files. It uploads extracted data here (like Parquet files) and downloads config from here. Think of it like a locker at school.
- **StateStore** -- A tiny notebook in the robot's pocket. It writes quick notes like "I was on page 5" so if it restarts, it remembers where it left off.
- **SecretStore** -- A locked safe. Passwords and API keys go here. Only the robot can open it.

### 6. The Eyes -- `Observability` (Logs, Metrics, Traces)

How do you know what the robot is doing out there? It has cameras and sensors!
- **Logs** -- The robot talks out loud: "I'm connecting now... I found 42 tables... Uh oh, that one's broken."
- **Metrics** -- The robot counts things: "I processed 1,000 rows. It took 3.2 seconds."
- **Traces** -- Like breadcrumbs. You can follow the trail to see exactly which path the robot took and where it got slow.

### 7. The Clipboard -- `Contracts` (Input/Output Models)

Every job has a form to fill out:
- **Input** -- "Here's what you need for the job" (connection details, which schemas to crawl, etc.)
- **Output** -- "Here's your report when you're done" (success/failure, how many things you found, any errors)

These are like standardized forms so every robot fills them out the same way, and headquarters can read any robot's report.

### 8. The Toolbelt -- `Transformers`

Once the robot grabs raw data, it might need to reshape it before delivering. Transformers convert messy data from the outside world into Atlan's format (Atlas entities). Like translating from French to English before handing in your report.

### 9. The Assembly Line -- `Outputs` (Collector)

When the robot finds metadata, it doesn't just dump everything on the floor. The **Output Collector** is like a conveyor belt -- the robot places items on it one by one, and the belt carries them to a neat pile (a file), ready for upload.

---

## How a Robot Mission Works (End to End)

```
Atlan says: "Go check out this Snowflake database!"
  |
  v
1. WALKIE-TALKIE (Handler) receives the request
  |
  v
2. BRAIN (App) reads the instructions and starts the TO-DO LIST (Workflow)
  |
  v
3. Step 1: ARMS (SQL Client) reach out to Snowflake -- "Can I connect? Yes!"
  |
  v
4. Step 2: ARMS crawl through databases, schemas, tables, columns
  |          |
  |          v
  |     TOOLBELT (Transformers) converts raw Snowflake metadata -> Atlan format
  |          |
  |          v
  |     ASSEMBLY LINE (Output Collector) queues up the results
  |
  v
5. Step 3: BACKPACK (ObjectStore) stores the results as files
  |
  v
6. Step 4: Results delivered to Atlan!
  |
  v
7. Step 5: Robot cleans up temp files and heads home.

Throughout: EYES (Observability) are recording everything.
           NOTEBOOK (StateStore) tracks progress.
           SAFE (SecretStore) keeps passwords locked up.
           TEACHER (Temporal) makes sure no step is skipped.
```

---

## Why Use the Kit Instead of Building From Scratch?

| Without the SDK (raw materials) | With the SDK (robot kit) |
|---|---|
| Write your own HTTP server | Handler gives you one |
| Build your own retry logic | Temporal handles retries |
| Figure out auth for every source | Clients come with auth built in |
| Invent your own file storage | ObjectStore is ready to go |
| Hope you remember to add logging | Observability is baked in |
| Handle crashes yourself | Heartbeats + state recovery |

It's the difference between building a robot from a pile of metal vs. snapping together LEGO Technic. Both make a robot, but one lets you focus on what makes YOUR robot special.

---

## One Last Thing -- The Two Modes

Your robot can run in two ways:

1. **Handler mode** -- Just the walkie-talkie. It sits and waits for Atlan to ask it questions ("Can you connect? What metadata is there?").
2. **Worker mode** -- The full mission. The robot goes out, follows the to-do list, extracts data, and brings it back.
3. **Combined mode** -- Both at once! Walkie-talkie AND mission runner in one robot. Great for local development.

---

*That's it! The Atlan Application SDK is a robot-building kit. It gives you pre-made parts (clients, handlers, storage, observability, workflows) so you can focus on the fun part -- teaching your robot where to go and what to bring back.*
