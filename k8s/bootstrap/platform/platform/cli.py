"""Main CLI entry point."""

import typer
from rich.console import Console

from platform.commands import bootstrap

app = typer.Typer(
    name="platform",
    help="CLI tool for managing stack-ai platform deployments",
    no_args_is_help=True,
)

console = Console()

# Register commands
app.add_typer(bootstrap.app, name="bootstrap")


@app.callback()
def main() -> None:
    """
    Platform CLI - Manage stack-ai Kubernetes deployments.

    Use 'platform bootstrap' to create new environments.
    """
    pass


if __name__ == "__main__":
    app()
