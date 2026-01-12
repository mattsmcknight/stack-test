# Infractl

CLI tool for managing stack-test infrastructure deployments.

## Installation

```bash
cd k8s/bootstrap/platform
pip install -e .
```

## Usage

### Bootstrap a dev environment

```bash
infractl bootstrap dev
```

### Bootstrap a prod environment

```bash
infractl bootstrap prod
```

### Options

```bash
infractl bootstrap dev --help

Options:
  -n, --cluster-name TEXT   EKS cluster name [default: stack-test-dev]
  -r, --region TEXT         AWS region [default: us-east-1]
  --skip-cluster-create     Skip EKS cluster creation (use existing cluster)
  --skip-git-push           Skip git commit and push
  --skip-sync               Skip ArgoCD sync orchestration
  --help                    Show this message and exit
```

### Examples

```bash
# Dev environment with custom name
infractl bootstrap dev --cluster-name my-dev-cluster

# Prod in different region
infractl bootstrap prod --region us-west-2

# Use existing cluster (skip eksctl create)
infractl bootstrap dev --skip-cluster-create

# Don't push to git (for testing)
infractl bootstrap dev --skip-git-push

# Skip automatic sync (let ArgoCD sync on its own schedule)
infractl bootstrap dev --skip-sync
```

## What Bootstrap Does

| Phase | Action |
|-------|--------|
| 1 | Creates EKS cluster + VPC via eksctl |
| 2 | Gathers AWS resource information (VPC, subnets, etc.) |
| 3 | Creates Crossplane IAM role with IRSA |
| 4 | Installs ArgoCD |
| 5 | Creates cluster-info ConfigMap |
| 6 | Generates import-existing.yaml for the overlay |
| 7 | Commits and pushes to git |
| 8 | Applies ArgoCD ApplicationSets |
| 9 | Syncs applications in dependency order |

Then ArgoCD takes over and syncs:
- Wave -3: Crossplane
- Wave -2: Crossplane Providers + External Secrets Operator
- Wave  0: Infrastructure + Supabase

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Type checking
mypy infractl

# Linting
ruff check infractl
```

## Project Structure

```
k8s/bootstrap/platform/
├── pyproject.toml              # Package config
├── README.md
├── cluster.yaml                # eksctl cluster template
├── permission-boundary.json    # IAM permission boundary
└── infractl/
    ├── cli.py                  # Main entry point
    ├── config.py               # Configuration dataclasses
    ├── commands/
    │   └── bootstrap.py        # Bootstrap command
    └── services/
        ├── argocd.py           # ArgoCD REST API operations
        ├── aws.py              # AWS operations (IAM, EC2, EKS)
        ├── eksctl.py           # eksctl operations
        ├── git.py              # Git operations
        └── kubernetes.py       # kubectl operations
```
