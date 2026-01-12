"""Kubernetes service interactions."""

import subprocess
import time
from pathlib import Path

import yaml
from rich.console import Console

from infractl.config import ClusterConfig

console = Console()


class KubernetesService:
    """Service for Kubernetes operations."""

    def namespace_exists(self, namespace: str) -> bool:
        """Check if a namespace exists."""
        result = subprocess.run(
            ["kubectl", "get", "namespace", namespace],
            capture_output=True,
        )
        return result.returncode == 0

    def create_namespace(self, namespace: str) -> None:
        """Create a namespace if it doesn't exist."""
        if self.namespace_exists(namespace):
            console.print(f"[yellow]Namespace {namespace} already exists[/yellow]")
            return

        subprocess.run(
            ["kubectl", "create", "namespace", namespace],
            check=True,
        )
        console.print(f"[green]Created namespace {namespace}[/green]")

    def apply_manifest(self, manifest: str | Path, namespace: str | None = None) -> None:
        """Apply a Kubernetes manifest."""
        cmd = ["kubectl", "apply"]
        if namespace:
            cmd.extend(["-n", namespace])

        if isinstance(manifest, Path):
            cmd.extend(["-f", str(manifest)])
        else:
            cmd.extend(["-f", "-"])

        subprocess.run(
            cmd,
            input=manifest if isinstance(manifest, str) else None,
            text=True,
            check=True,
        )

    def apply_url(self, url: str, namespace: str | None = None) -> None:
        """Apply a manifest from a URL."""
        cmd = ["kubectl", "apply", "-f", url]
        if namespace:
            cmd.extend(["-n", namespace])

        subprocess.run(cmd, check=True)

    def wait_for_deployment(
        self, deployment: str, namespace: str, timeout: int = 300
    ) -> None:
        """Wait for a deployment to be available."""
        console.print(f"[blue]Waiting for {deployment} to be ready...[/blue]")
        subprocess.run(
            [
                "kubectl", "wait",
                "--for=condition=available",
                f"--timeout={timeout}s",
                f"deployment/{deployment}",
                "-n", namespace,
            ],
            check=True,
        )

    def create_cluster_info_configmap(self, config: ClusterConfig) -> None:
        """Create the cluster-info ConfigMap."""
        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "cluster-info",
                "namespace": "crossplane-system",
            },
            "data": {
                "account_id": config.account_id,
                "region": config.region,
                "environment": config.environment.value,
                "cluster_name": config.cluster_name,
                "vpc_id": config.vpc_id,
                "oidc_provider": config.oidc_provider,
                "oidc_id": config.oidc_id,
                "crossplane_role_arn": config.crossplane_role_arn,
                "private_subnet_a": config.private_subnets.get("a", ""),
                "private_subnet_b": config.private_subnets.get("b", ""),
                "private_subnet_c": config.private_subnets.get("c", ""),
                "public_subnet_a": config.public_subnets.get("a", ""),
                "public_subnet_b": config.public_subnets.get("b", ""),
                "public_subnet_c": config.public_subnets.get("c", ""),
                "igw_id": config.igw_id,
                "nat_id": config.nat_id,
            },
        }

        self.apply_manifest(yaml.dump(configmap))
        console.print("[green]Created cluster-info ConfigMap[/green]")

    def install_argocd(self, version: str = "v2.10.0") -> None:
        """Install ArgoCD."""
        self.create_namespace("argocd")

        console.print("[blue]Installing ArgoCD...[/blue]")
        self.apply_url(
            f"https://raw.githubusercontent.com/argoproj/argo-cd/{version}/manifests/install.yaml",
            namespace="argocd",
        )

        self.wait_for_deployment("argocd-server", "argocd")
        console.print("[green]ArgoCD installed successfully[/green]")

    def apply_applicationsets(self, argocd_path: Path) -> None:
        """Apply ArgoCD project and applicationsets."""
        self.apply_manifest(argocd_path / "base" / "project.yaml")
        self.apply_manifest(argocd_path / "base" / "applicationsets.yaml")
        console.print("[green]Applied ArgoCD ApplicationSets[/green]")
