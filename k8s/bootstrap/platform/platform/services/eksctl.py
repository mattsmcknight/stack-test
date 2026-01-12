"""eksctl service interactions."""

import subprocess
from pathlib import Path

from rich.console import Console

console = Console()


class EksctlService:
    """Service for eksctl operations."""

    def __init__(self, region: str) -> None:
        self.region = region

    def cluster_exists(self, cluster_name: str) -> bool:
        """Check if an EKS cluster exists."""
        result = subprocess.run(
            ["eksctl", "get", "cluster", "--name", cluster_name, "--region", self.region],
            capture_output=True,
        )
        return result.returncode == 0

    def create_cluster(self, cluster_name: str, config_file: Path) -> None:
        """Create an EKS cluster using eksctl."""
        if self.cluster_exists(cluster_name):
            console.print(f"[yellow]Cluster {cluster_name} already exists[/yellow]")
            return

        console.print(f"[blue]Creating EKS cluster {cluster_name}...[/blue]")
        subprocess.run(
            ["eksctl", "create", "cluster", "-f", str(config_file)],
            check=True,
        )
        console.print(f"[green]Cluster {cluster_name} created successfully[/green]")

    def update_cluster_config(
        self, config_file: Path, cluster_name: str, region: str
    ) -> None:
        """Update the eksctl cluster.yaml with name and region."""
        content = config_file.read_text()
        content = content.replace("name: stack-ai", f"name: {cluster_name}")
        content = content.replace("region: us-east-1", f"region: {region}")
        config_file.write_text(content)
