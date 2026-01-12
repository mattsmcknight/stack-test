"""Configuration management."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Environment(str, Enum):
    """Deployment environment."""
    DEV = "dev"
    PROD = "prod"


@dataclass
class ClusterConfig:
    """Configuration for cluster bootstrap."""

    environment: Environment
    cluster_name: str = "stack-test"
    region: str = "us-east-1"

    # Derived values (populated during bootstrap)
    account_id: str = ""
    vpc_id: str = ""
    oidc_provider: str = ""
    oidc_id: str = ""

    # Subnet IDs
    private_subnets: dict[str, str] = field(default_factory=dict)
    public_subnets: dict[str, str] = field(default_factory=dict)

    # Other resource IDs
    igw_id: str = ""
    nat_id: str = ""

    @property
    def crossplane_role_name(self) -> str:
        return "crossplane-provider-aws"

    @property
    def crossplane_role_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:role/{self.crossplane_role_name}"

    @property
    def permission_boundary_name(self) -> str:
        return "crossplaneBoundary"

    @property
    def permission_boundary_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:policy/{self.permission_boundary_name}"

    @property
    def argocd_secret_name(self) -> str:
        return f"{self.cluster_name}/argocd/admin"


@dataclass
class Paths:
    """Project paths."""

    root: Path

    @property
    def k8s(self) -> Path:
        return self.root / "k8s"

    @property
    def infrastructure(self) -> Path:
        return self.k8s / "infrastructure"

    @property
    def argocd(self) -> Path:
        return self.k8s / "argocd"

    @property
    def platform(self) -> Path:
        return self.k8s / "bootstrap" / "platform"

    @property
    def eksctl_config(self) -> Path:
        return self.platform / "cluster.yaml"

    @property
    def permission_boundary(self) -> Path:
        return self.platform / "permission-boundary.json"

    def overlay(self, env: Environment) -> Path:
        return self.infrastructure / "overlays" / env.value / "patches"

    @classmethod
    def from_git_root(cls) -> "Paths":
        """Create Paths from git repository root."""
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return cls(root=Path(result.stdout.strip()))
