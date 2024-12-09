# Deploying Atlan App and Atlan App Components via Helmfile

This repository contains two Helm charts: `app` and `atlan`. These charts facilitate the deployment of Atlan's infrastructure and application services. This guide provides instructions for deploying these charts using Helmfile and setting up a local development environment using a `k3d` Kubernetes cluster.

**Table of Contents**
- [Chart Overview](#chart-overview)
- [Pre-requisites](#pre-requisites)
- [Deployment Steps](#deployment-steps)
- [Testing Locally](#testing-locally)
    - [Verify Services](#verify-services)
    - [Bucket Configuration](#bucket-configuration)

## Chart Overview
**1. Atlan**

- **Chart Name:** atlan
- **Description:** Deploys foundational components required for Atlan's App ecosystem, such as Temporal, MinIO (For dev environment as an objectstore), Dapr, and PostgreSQL.
- **Version:** 0.1.0
- **Key Features:**
    - Includes dependencies for services like Temporal Operator, Cert Manager, and CloudNative PostgreSQL.
    - Configurable namespaces and options for enabling/disabling specific components like MinIO.

**2. Dapr Shared**

- **Chart Name:** dapr-shared-chart
- **Description:** Deploys the shared Dapr instance which can be used by across the cluster.
- **Version:** 0.0.15
- **Key Features:**
    - Deploys a shared, centralized Dapr runtime instance that can be accessed by applications across the Kubernetes cluster.
    - Eliminates the need for sidecar-based Dapr deployment in individual pods.

**3. App**

- **Chart Name:** app
- **Description:** Deploys the Atlan application by leveraging atlan as a dependency.
- **Version:** 0.1.0
- **Key Features:**
    - Includes business logic and application services.
    - Dependency-driven approach to ensure components from atlan-app-components are provisioned before deploying the application.

## Pre-requisites
- **Install Helm**
    ```bash
    brew install helm
    ```

- **Install Helmfile**
    ```bash
    brew install helmfile
    ```

- **Install k3d (if not installed)**
    ```bash
    brew install k3d
    ```

- **Start a Local Development Cluster**
    ```bash
    k3d cluster create atlan-cluster --api-port 6550 --port 80:80@loadbalancer --port 443:443@loadbalancer
    ```

## Deployment Steps
**Step 1: Deploy Atlan App And Its Components**

- Navigate to the `atlanhq` directory:
    ```bash
    cd atlanhq
    ```

- Use `helmfile` to deploy the components and app together. By default this will install the helm charts in the respective namepscaes for the services.
    ```bash
    helmfile apply
    ```
    - If you want to install each components and app all in a single namespace, but this will require an update in all `values.yaml` files of the charts, run the below command:
        ```bash
        helmfile apply -e unified
        ```
    - If you just want to install the app and make sure other components are already deployed, run the below command:
        ```bash
        helmfile apply -e app
        ```

- This will:
    - Install foundational components like Temporal Operator, Dapr, MinIO, and PostgreSQL.
    - Wait for services to be ready before proceeding with subsequent deployment Temporal Cluster.

**Step 2: Deploy Atlan App**

- Navigate to the `atlan_apps` directory:
    ```bash
    cd atlan_apps
    ```

- Ensure the dependency `atlan-app-components` is referenced correctly:
    - The `dependencies` section in `Chart.yaml` should point to `../atlan_app_components`.

- Install the `atlan-apps` chart using `helmfile` for sample Hello World App:
    ```bash
    helmfile apply
    ```

- This will:
    - Deploy the Atlan application and its services and dapr components.
    - Ensure components from `atlan-app-components` are deployed beforehand.

## Testing Locally
### Verify Services

To test and interact with the application and its services, use port forwarding commands to access specific services:

- **Atlan Application**
    ```bash
    kubectl port-forward svc/hello-world-app 8000:8000
    ```
    - Access the Atlan application at: http://localhost:8000

- **Temporal UI**
    ```bash
    kubectl port-forward svc/temporal-cluster-ui 8080:8080 -n atlan-temporal
    ```
    - Access Temporal UI at: http://localhost:8080

- **MinIO Console**
    ```bash
    kubectl port-forward svc/myminio-console 9443:9443 -n atlan-minio-tenant
    ```
    - Access the MinIO console at: https://localhost:9443

### Bucket Configuration
- By default, a bucket named app-objects is created in the MinIO tenant during deployment.
    ```yaml
    buckets:
        - name: app-objects
        objectLock: false
        region: us-east-1
    ```
- This bucket is used by the objectstore component in Dapr for storing application data.
