"""ArgoCD service interactions."""

import subprocess
import time
from typing import Literal

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

AppStatus = Literal["Healthy", "Progressing", "Degraded", "Suspended", "Missing", "Unknown"]


class ArgoCDService:
    """Service for ArgoCD operations."""

    def get_app_status(self, app_name: str) -> tuple[str, str]:
        """Get the sync and health status of an application."""
        result = subprocess.run(
            [
                "kubectl", "get", "application", app_name,
                "-n", "argocd",
                "-o", "jsonpath={.status.sync.status},{.status.health.status}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return "Unknown", "Unknown"

        parts = result.stdout.strip().split(",")
        sync_status = parts[0] if len(parts) > 0 else "Unknown"
        health_status = parts[1] if len(parts) > 1 else "Unknown"
        return sync_status, health_status

    def app_exists(self, app_name: str) -> bool:
        """Check if an application exists."""
        result = subprocess.run(
            ["kubectl", "get", "application", app_name, "-n", "argocd"],
            capture_output=True,
        )
        return result.returncode == 0

    def sync_app(self, app_name: str, timeout: int = 300) -> bool:
        """Trigger a sync for an application."""
        console.print(f"[blue]Syncing {app_name}...[/blue]")

        # Trigger sync via annotation refresh
        subprocess.run(
            [
                "kubectl", "patch", "application", app_name,
                "-n", "argocd",
                "--type", "merge",
                "-p", '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}',
            ],
            check=True,
        )

        # Also trigger actual sync operation
        subprocess.run(
            [
                "kubectl", "patch", "application", app_name,
                "-n", "argocd",
                "--type", "merge",
                "-p", '{"operation":{"initiatedBy":{"username":"platform-cli"},"sync":{}}}',
            ],
            capture_output=True,  # Don't fail if operation already in progress
        )

        return True

    def wait_for_health(
        self,
        app_name: str,
        timeout: int = 600,
        target_health: str = "Healthy",
    ) -> bool:
        """Wait for an application to reach target health status."""
        start_time = time.time()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Waiting for {app_name}...", total=None)

            while time.time() - start_time < timeout:
                sync_status, health_status = self.get_app_status(app_name)
                progress.update(
                    task,
                    description=f"Waiting for {app_name} (sync: {sync_status}, health: {health_status})",
                )

                if health_status == target_health and sync_status == "Synced":
                    console.print(f"[green]{app_name} is {health_status}[/green]")
                    return True

                if health_status == "Degraded":
                    console.print(f"[red]{app_name} is Degraded[/red]")
                    return False

                time.sleep(5)

        console.print(f"[yellow]Timeout waiting for {app_name}[/yellow]")
        return False

    def enable_auto_sync(self, app_name: str) -> None:
        """Enable auto-sync for an application."""
        subprocess.run(
            [
                "kubectl", "patch", "application", app_name,
                "-n", "argocd",
                "--type", "merge",
                "-p", '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}',
            ],
            check=True,
        )
        console.print(f"[green]Enabled auto-sync for {app_name}[/green]")

    def sync_in_order(self, env: str) -> bool:
        """
        Sync applications in the correct dependency order.

        Order:
        1. crossplane - Install Crossplane itself
        2. crossplane-providers - Install AWS/Helm/K8s providers
        3. infrastructure - Create Aurora, Route53, ESO, etc.
        4. supabase - Deploy the application
        """
        apps = [
            (f"crossplane-{env}", 300, True),      # (name, timeout, enable_auto_sync)
            (f"crossplane-providers-{env}", 600, True),
            (f"infrastructure-{env}", 900, True),  # Aurora can take a while
            (f"supabase-{env}", 300, True),
        ]

        console.print(f"\n[bold]Syncing applications for {env} environment[/bold]")
        console.print("Order: crossplane → providers → infrastructure → supabase\n")

        for app_name, timeout, enable_auto in apps:
            # Wait for app to exist (ApplicationSet may take a moment)
            retries = 0
            while not self.app_exists(app_name) and retries < 12:
                time.sleep(5)
                retries += 1

            if not self.app_exists(app_name):
                console.print(f"[red]Application {app_name} not found[/red]")
                return False

            # Sync and wait
            self.sync_app(app_name)
            if not self.wait_for_health(app_name, timeout):
                console.print(f"[red]Failed to sync {app_name}[/red]")
                console.print("[yellow]You may need to check ArgoCD UI for details[/yellow]")
                return False

            # Enable auto-sync after successful initial sync
            if enable_auto:
                self.enable_auto_sync(app_name)

        console.print("\n[green]All applications synced successfully![/green]")
        return True
