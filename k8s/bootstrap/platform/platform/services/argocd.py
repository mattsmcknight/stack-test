"""ArgoCD service interactions."""

import base64
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Generator

import requests
from kubernetes import client, config
from kubernetes.stream import portforward
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


class _PortForwarder:
    """Local TCP server that forwards connections through kubernetes portforward."""

    def __init__(self, pf: Any, remote_port: int, local_port: int = 0) -> None:
        self._pf = pf
        self._remote_port = remote_port
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.settimeout(1.0)
        self._server.bind(("127.0.0.1", local_port))
        self._server.listen(5)
        self._local_port = self._server.getsockname()[1]
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def local_port(self) -> int:
        return self._local_port

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        time.sleep(0.1)  # Give the server a moment to start

    def stop(self) -> None:
        self._running = False
        self._server.close()
        if self._thread:
            self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()
                threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    daemon=True,
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_connection(self, local_conn: socket.socket) -> None:
        try:
            pf_socket = self._pf.socket(self._remote_port)
            # Start bidirectional forwarding
            t = threading.Thread(
                target=self._forward,
                args=(local_conn, pf_socket),
                daemon=True,
            )
            t.start()
            self._forward(pf_socket, local_conn)
            t.join(timeout=1)
        except Exception:
            pass
        finally:
            try:
                local_conn.close()
            except Exception:
                pass

    def _forward(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while self._running:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass


class ArgoCDService:
    """Service for ArgoCD operations via REST API."""

    def __init__(self) -> None:
        self._token: str | None = None
        config.load_kube_config()
        self._v1 = client.CoreV1Api()

    def _get_admin_password(self) -> str:
        """Get the ArgoCD admin password from the secret."""
        secret = self._v1.read_namespaced_secret(
            name="argocd-initial-admin-secret",
            namespace="argocd",
        )
        return base64.b64decode(secret.data["password"]).decode()

    @contextmanager
    def _port_forward(self, local_port: int = 8080) -> Generator[str, None, None]:
        """Context manager for port-forwarding to ArgoCD server."""
        # Find argocd-server pod
        pods = self._v1.list_namespaced_pod(
            namespace="argocd",
            label_selector="app.kubernetes.io/name=argocd-server",
        )
        if not pods.items:
            raise RuntimeError("No argocd-server pod found")

        pod_name = pods.items[0].metadata.name

        # Create kubernetes portforward
        pf = portforward(
            self._v1.connect_get_namespaced_pod_portforward,
            pod_name,
            "argocd",
            ports="8443",
        )

        # Create local proxy server
        forwarder = _PortForwarder(pf, 8443, local_port)
        forwarder.start()

        try:
            yield f"https://localhost:{forwarder.local_port}"
        finally:
            forwarder.stop()

    def _get_token(self, base_url: str) -> str:
        """Get an auth token from ArgoCD."""
        if self._token:
            return self._token

        password = self._get_admin_password()

        response = requests.post(
            f"{base_url}/api/v1/session",
            json={"username": "admin", "password": password},
            verify=False,  # Self-signed cert
            timeout=10,
        )
        response.raise_for_status()

        self._token = response.json()["token"]
        return self._token

    def _api_request(
        self,
        base_url: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an authenticated API request."""
        token = self._get_token(base_url)
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        response = requests.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            verify=False,
            timeout=30,
            **kwargs,
        )
        return response

    def get_app(self, base_url: str, app_name: str) -> dict[str, Any] | None:
        """Get application details."""
        response = self._api_request(base_url, "GET", f"/api/v1/applications/{app_name}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def get_app_status(self, base_url: str, app_name: str) -> tuple[str, str]:
        """Get the sync and health status of an application."""
        app = self.get_app(base_url, app_name)
        if not app:
            return "Unknown", "Unknown"

        status = app.get("status", {})
        sync_status = status.get("sync", {}).get("status", "Unknown")
        health_status = status.get("health", {}).get("status", "Unknown")
        return sync_status, health_status

    def sync_app(self, base_url: str, app_name: str) -> bool:
        """Trigger a sync for an application."""
        console.print(f"[blue]Syncing {app_name}...[/blue]")

        response = self._api_request(
            base_url,
            "POST",
            f"/api/v1/applications/{app_name}/sync",
            json={
                "prune": True,
                "strategy": {"apply": {"force": False}},
            },
        )

        if response.status_code in (200, 201):
            return True

        console.print(f"[yellow]Sync request returned {response.status_code}[/yellow]")
        return False

    def wait_for_health(
        self,
        base_url: str,
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
                sync_status, health_status = self.get_app_status(base_url, app_name)
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

    def enable_auto_sync(self, base_url: str, app_name: str) -> None:
        """Enable auto-sync for an application."""
        # Get current app spec
        app = self.get_app(base_url, app_name)
        if not app:
            console.print(f"[red]Application {app_name} not found[/red]")
            return

        # Update sync policy
        response = self._api_request(
            base_url,
            "PATCH",
            f"/api/v1/applications/{app_name}",
            headers={"Content-Type": "application/merge-patch+json"},
            json={
                "spec": {
                    "syncPolicy": {
                        "automated": {
                            "prune": True,
                            "selfHeal": True,
                        }
                    }
                }
            },
        )

        if response.status_code == 200:
            console.print(f"[green]Enabled auto-sync for {app_name}[/green]")
        else:
            console.print(f"[yellow]Failed to enable auto-sync: {response.status_code}[/yellow]")

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
            (f"crossplane-{env}", 300),
            (f"crossplane-providers-{env}", 600),
            (f"infrastructure-{env}", 900),  # Aurora can take a while
            (f"supabase-{env}", 300),
        ]

        console.print(f"\n[bold]Syncing applications for {env} environment[/bold]")
        console.print("Order: crossplane -> providers -> infrastructure -> supabase\n")

        # Suppress SSL warnings for self-signed cert
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        with self._port_forward() as base_url:
            for app_name, timeout in apps:
                # Wait for app to exist (ApplicationSet may take a moment)
                retries = 0
                while self.get_app(base_url, app_name) is None and retries < 12:
                    console.print(f"[dim]Waiting for {app_name} to be created...[/dim]")
                    time.sleep(5)
                    retries += 1

                if self.get_app(base_url, app_name) is None:
                    console.print(f"[red]Application {app_name} not found[/red]")
                    return False

                # Sync and wait
                self.sync_app(base_url, app_name)
                if not self.wait_for_health(base_url, app_name, timeout):
                    console.print(f"[red]Failed to sync {app_name}[/red]")
                    console.print("[yellow]Check ArgoCD UI for details[/yellow]")
                    return False

                # Enable auto-sync after successful initial sync
                self.enable_auto_sync(base_url, app_name)

        console.print("\n[green]All applications synced successfully![/green]")
        return True
