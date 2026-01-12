"""Main CLI entry point."""

import typer
from rich.console import Console

from infractl.commands import bootstrap

app = typer.Typer(
    name="infractl",
    help="CLI tool for managing stack-test infrastructure deployments",
    no_args_is_help=True,
)

console = Console()

# Register commands
app.add_typer(bootstrap.app, name="bootstrap")


@app.callback()
def main() -> None:
    """
    Infractl - Manage stack-test Kubernetes infrastructure.

    Use 'infractl bootstrap' to create new environments.
    """
    pass


if __name__ == "__main__":
    app()
