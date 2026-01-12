# Supabase on EKS with Crossplane

A GitOps-based deployment of Supabase on AWS EKS using Crossplane for infrastructure provisioning and ArgoCD for continuous delivery.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         EKS Cluster                             │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │   ArgoCD     │  │  Crossplane  │  │   External Secrets    │  │
│  │              │  │              │  │      Operator         │  │
│  │  Watches Git │  │ Provisions   │  │                       │  │
│  │  Syncs Apps  │  │ AWS Resources│  │  Syncs secrets from   │  │
│  │              │  │              │  │  AWS Secrets Manager  │  │
│  └──────────────┘  └──────────────┘  └───────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                      Supabase                            │   │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────────────┐  │   │
│  │  │  Kong   │ │ GoTrue  │ │ Realtime│ │   PostgREST    │  │   │
│  │  │ (API GW)│ │ (Auth)  │ │         │ │                │  │   │
│  │  └─────────┘ └─────────┘ └─────────┘ └────────────────┘  │   │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────────────┐  │   │
│  │  │ Storage │ │  Meta   │ │ Studio  │ │   Functions    │  │   │
│  │  │         │ │         │ │  (UI)   │ │                │  │   │
│  │  └─────────┘ └─────────┘ └─────────┘ └────────────────┘  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     AWS Resources                               │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ Aurora       │  │    S3        │  │   Secrets Manager     │  │
│  │ PostgreSQL   │  │   Bucket     │  │                       │  │
│  └──────────────┘  └──────────────┘  └───────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- AWS CLI configured with appropriate credentials
- kubectl
- eksctl
- Python 3.10+

## Quick Start

### 1. Install the CLI

```bash
cd k8s/bootstrap/platform
pip install -e .
```

### 2. Bootstrap a dev environment

```bash
infractl bootstrap dev
```

This will:
1. Create an EKS cluster with eksctl
2. Install ArgoCD
3. Configure Crossplane IAM roles
4. Apply ArgoCD ApplicationSets
5. Sync all applications in dependency order

### 3. Access ArgoCD UI

```bash
# Get the admin password (stored in AWS Secrets Manager)
aws secretsmanager get-secret-value --secret-id <cluster-name>/argocd/admin --query SecretString --output text

# Port forward
kubectl port-forward svc/argocd-server -n argocd 8080:443
```

Open https://localhost:8080 (username: `admin`)

## Project Structure

```
k8s/
├── argocd/                     # ArgoCD configuration
│   └── base/
│       ├── project.yaml        # ArgoCD project definition
│       └── applicationsets.yaml # ApplicationSet templates
│
├── bootstrap/
│   └── platform/               # infractl CLI tool
│       ├── infractl/           # Python package
│       └── pyproject.toml
│
├── crossplane/                 # Crossplane installation
│   └── base/
│       └── crossplane.yaml     # Crossplane Helm release
│
├── crossplane-providers/       # Crossplane AWS providers
│   └── base/
│       ├── providers.yaml      # Provider installations
│       └── provider-config.yaml # IRSA configuration
│
├── infrastructure/             # AWS infrastructure via Crossplane
│   ├── base/
│   │   ├── aurora.yaml         # Aurora PostgreSQL cluster
│   │   ├── s3.yaml             # S3 bucket for storage
│   │   └── eso.yaml            # External Secrets Operator
│   └── overlays/
│       ├── dev/                # Dev environment config
│       └── prod/               # Prod environment config
│
└── supabase/                   # Supabase deployment
    ├── base/
    │   ├── configmap.yaml      # Supabase configuration
    │   ├── secrets.yaml        # External secrets references
    │   └── *.yaml              # Service deployments
    └── overlays/
        ├── dev/
        └── prod/
```

## How It Works

### GitOps Flow

1. **Bootstrap** creates the EKS cluster and installs ArgoCD
2. **ArgoCD** watches this Git repository and syncs changes
3. **Crossplane** provisions AWS infrastructure (Aurora, S3, etc.)
4. **External Secrets Operator** syncs secrets from AWS Secrets Manager
5. **Supabase** connects to the provisioned infrastructure

### Sync Order

Applications are synced in dependency order using sync waves:

| Wave | Application | Description |
|------|-------------|-------------|
| -3 | Crossplane | Core Crossplane installation |
| -2 | Crossplane Providers | AWS provider + ESO |
| 0 | Infrastructure | Aurora, S3, Route53 |
| 0 | Supabase | Application deployment |

### Environment Overlays

The project uses Kustomize overlays for environment-specific configuration:

- **base/** - Common configuration shared across environments
- **overlays/dev/** - Development environment (smaller instances, relaxed settings)
- **overlays/prod/** - Production environment (HA, larger instances, strict settings)

## CLI Reference

```bash
# Bootstrap dev environment
infractl bootstrap dev

# Bootstrap prod environment
infractl bootstrap prod

# Custom cluster name and region
infractl bootstrap dev --cluster-name my-cluster --region us-west-2

# Use existing cluster (skip eksctl)
infractl bootstrap dev --skip-cluster-create

# Skip git push (for testing)
infractl bootstrap dev --skip-git-push

# Skip ArgoCD sync orchestration
infractl bootstrap dev --skip-sync
```

## Development

```bash
# Install dev dependencies
cd k8s/bootstrap/platform
pip install -e ".[dev]"

# Type checking
mypy infractl

# Linting
ruff check infractl
```
