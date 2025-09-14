# 📘 Concepts Overview

This folder contains conceptual and reference documentation for key modules and abstractions within the Atlan Application SDK. Each file explains a different part of the system to help developers understand how to build and extend applications using the SDK.

Below is a quick guide to each document in this folder:

---

### ⚙️ Core Architecture

- [`application.md`](application.md)  
  Overview of the core `Application` abstraction for orchestrating workflows, workers, and (optionally) servers.

- [`application_sql.md`](application_sql.md)  
  High-level abstraction for building **SQL metadata extraction** apps with Temporal and FastAPI.

- [`server.md`](server.md)  
  Describes how to expose workflows and handlers via a FastAPI-powered server.

- [`worker.md`](worker.md)  
  Details the `Worker` class that listens to Temporal queues and runs workflow/activity logic.

- [`temporal_auth.md`](temporal_auth.md)  
  Covers the **authentication mechanisms** for Temporal workers.

---

### 🔁 Workflow Orchestration

- [`workflows.md`](workflows.md)  
  Defines orchestration logic using Temporal **Workflows**.

- [`activities.md`](activities.md)  
  Building blocks for defining **Activities**-the individual units of work inside a workflow.

- [`handlers.md`](handlers.md)  
  System-specific components that connect SDK workflows to external systems (e.g., databases, APIs).

---

### 🔌 Inputs & Outputs

- [`inputs.md`](inputs.md)  
  Standardized ways to **read data** from sources like SQL, Parquet, or JSON files.

- [`outputs.md`](outputs.md)  
  Standardized ways to **write data** to destinations in formats like JSON Lines or Parquet.

- [`output_paths.md`](output_paths.md)  
  Describes path templates used to organize workflow outputs and state data.

---

### 🌐 External Integration

- [`clients.md`](clients.md)  
  Abstractions for interacting with external systems like databases or orchestration engines.

- [`services.md`](services.md)  
  Interfaces for interacting with **state, storage, secrets**, and **event systems** via Dapr.

---

### 📦 Utilities & Extensions

- [`common.md`](common.md)  
  Utility functions and shared logic: logging, config, AWS integration, etc.

- [`atlanupload.md`](atlanupload.md)  
  Describes the **Atlan upload activity**, an extension point in the SDK.



