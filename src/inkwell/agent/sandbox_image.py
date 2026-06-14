"""Provisioned sandbox image for academic output — pandoc + tectonic + poppler.

The base uv image can't run the LaTeX toolchain: ``tectonic`` is a static Rust
binary, not an apt package on bookworm-slim. Academic runs therefore use an
image built ahead of time from ``sandbox.Dockerfile``. Build it once with
``uv run lup-devtools dev build-sandbox-image``; ``start_sandbox`` picks it up
automatically when it exists.
"""

import logging
from pathlib import Path

import docker
from docker.errors import DockerException

logger = logging.getLogger(__name__)

INKWELL_SANDBOX_IMAGE = "inkwell-sandbox:latest"
SANDBOX_DOCKERFILE = Path(__file__).parent / "sandbox.Dockerfile"


def sandbox_image_available(tag: str = INKWELL_SANDBOX_IMAGE) -> bool:
    """True when the provisioned sandbox image exists locally."""
    try:
        docker.from_env().images.get(tag)
    except DockerException:
        return False
    return True


def build_sandbox_image(tag: str = INKWELL_SANDBOX_IMAGE) -> None:
    """Build the academic sandbox image from ``sandbox.Dockerfile``."""
    import sh

    logger.info("Building %s — downloads tectonic, takes a few minutes", tag)
    docker_cmd = sh.Command("docker")
    docker_cmd(
        "build",
        "-f",
        str(SANDBOX_DOCKERFILE),
        "-t",
        tag,
        str(SANDBOX_DOCKERFILE.parent),
        _fg=True,
    )
