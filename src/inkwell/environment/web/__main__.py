"""Entry point for the Inkwell web server.

Usage:
    uv run inkwell-web
    uv run inkwell-web --port 8080
    uv run inkwell-web --reload
"""

import logging
import socket
from pathlib import Path

import sh
import typer
import uvicorn

app = typer.Typer(add_completion=False)

FRONTEND_DIR = Path(__file__).parent / "frontend"
DEFAULT_PORT = 4545


def build_frontend() -> None:
    if not (FRONTEND_DIR / "package.json").exists():
        return
    typer.echo("Building frontend…")
    sh.Command("npm")("run", "build", _cwd=str(FRONTEND_DIR))


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


@app.callback(invoke_without_command=True)
def main(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(DEFAULT_PORT, help="Bind port (0 = random available)"),
    reload: bool = typer.Option(False, help="Enable auto-reload for development"),
    skip_build: bool = typer.Option(
        False, "--skip-build", help="Skip frontend rebuild"
    ),
) -> None:
    """Start the Inkwell web server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not skip_build:
        build_frontend()
    if port != 0 and not port_available(host, port):
        typer.echo(f"Port {port} is taken, using a random available port.")
        port = 0
    uvicorn.run(
        "inkwell.environment.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        reload_dirs=["src"] if reload else None,
    )


if __name__ == "__main__":
    app()
