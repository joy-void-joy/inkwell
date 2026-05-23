"""Centralized path constants and helpers for agent session data.

Pure path layout — where things go on disk. No data discovery or disk
iteration; see :mod:`lup.lib.history` for cross-version queries.

Paths auto-detect the project root (walking up to ``pyproject.toml``)
but can be overridden via :func:`configure`::

    from lup.paths import configure
    configure(root=Path("/my/project"), notes_dir=Path("/my/data/notes"))

Layout:
    notes/traces/<version>/sessions/<session_id>/<timestamp>.json
    notes/traces/<version>/outputs/<task_id>/<timestamp>/
    notes/traces/<version>/logs/<session_id>/<timestamp>.md
    notes/feedback_loop/

Examples:
    Override paths for testing::

        >>> from lup.paths import configure, sessions_dir, project_root
        >>> configure(root=Path("/tmp/test-project"))
        >>> project_root()
        PosixPath('/tmp/test-project')
        >>> sessions_dir()
        PosixPath('/tmp/test-project/notes/traces/0.1.0/sessions')

    Check if a path is within allowed directories::

        >>> path_is_under("/data/sessions/12345/out.json", [Path("/data/sessions")])
        True
        >>> path_is_under("/etc/passwd", [Path("/data/sessions")])
        False

    Extract the directory prefix from a glob pattern::

        >>> extract_glob_dir("/tmp/foo/**/*.py")
        '/tmp/foo'
        >>> extract_glob_dir("**/*.py")
        ''
"""

import re
from datetime import datetime
from pathlib import Path

import tomllib


def find_project_root() -> Path:
    """Find project root by walking up to the pyproject.toml with [tool.lup]."""
    current = Path(__file__).resolve().parent
    for parent in [current, *current.parents]:
        pyproject = parent / "pyproject.toml"
        if pyproject.exists():
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
            if "lup" in data.get("tool", {}):
                return parent
    raise RuntimeError(
        "Could not find project root (no pyproject.toml with [tool.lup] found)"
    )


def read_agent_version(root: Path) -> str:
    """Read agent_version from [tool.lup] in pyproject.toml."""
    pyproject = root / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return data.get("tool", {}).get("lup", {}).get("agent_version", "0.0.0")


# -- Mutable path state -------------------------------------------------------
# Auto-detected on first import; overridable via configure().

PROJECT_ROOT = find_project_root()
AGENT_VERSION = read_agent_version(PROJECT_ROOT)
NOTES_DIR = PROJECT_ROOT / "notes"
RUNTIME_LOGS_DIR = PROJECT_ROOT / "logs"


def configure(
    *,
    root: Path | None = None,
    notes_dir: Path | None = None,
    logs_dir: Path | None = None,
    version: str | None = None,
) -> None:
    """Override auto-detected paths.

    Call before any session operations. All derived paths
    (``traces_path``, ``feedback_path``, etc.)
    update automatically since they read from these values.

    Args:
        root: Project root directory. Resets ``notes_dir``,
            ``logs_dir``, and ``version`` to values derived from
            the new root unless they are also specified.
        notes_dir: Override notes directory independently.
        logs_dir: Override runtime logs directory independently.
        version: Override agent version (read from [tool.lup] by default).
    """
    global PROJECT_ROOT, AGENT_VERSION, NOTES_DIR, RUNTIME_LOGS_DIR  # noqa: PLW0603

    if root is not None:
        PROJECT_ROOT = root
        AGENT_VERSION = read_agent_version(root)
        NOTES_DIR = root / "notes"
        RUNTIME_LOGS_DIR = root / "logs"

    if notes_dir is not None:
        NOTES_DIR = notes_dir
    if logs_dir is not None:
        RUNTIME_LOGS_DIR = logs_dir
    if version is not None:
        AGENT_VERSION = version


# -- Public path accessors ----------------------------------------------------


def project_root() -> Path:
    """Return the project root directory."""
    return PROJECT_ROOT


def agent_version() -> str:
    """Return the agent version from [tool.lup] in pyproject.toml."""
    return AGENT_VERSION


def notes_path() -> Path:
    """Return the notes directory (``<root>/notes`` by default)."""
    return NOTES_DIR


def runtime_logs_path() -> Path:
    """Return the runtime logs directory (``<root>/logs`` by default)."""
    return RUNTIME_LOGS_DIR


def traces_path() -> Path:
    """Return ``notes/traces/``."""
    return NOTES_DIR / "traces"


def feedback_path() -> Path:
    """Return ``notes/feedback_loop/``."""
    return NOTES_DIR / "feedback_loop"


# -- Timestamp helpers --------------------------------------------------------

TIMESTAMP_FMT = "%Y%m%d_%H%M%S"
TIMESTAMP_RE = re.compile(r"\d{8}_\d{6}")


def parse_timestamp(name: str) -> datetime:
    """Parse the last YYYYMMDD_HHMMSS occurrence from a filename or string."""
    matches = TIMESTAMP_RE.findall(Path(name).stem)
    if not matches:
        raise ValueError(f"No YYYYMMDD_HHMMSS timestamp found in: {name}")
    return datetime.strptime(matches[-1], TIMESTAMP_FMT)


# -- Write paths (version-specific) ------------------------------------------


def sessions_dir(version: str | None = None) -> Path:
    """Directory for session JSONs: notes/traces/<version>/sessions/"""
    return traces_path() / (version or AGENT_VERSION) / "sessions"


def outputs_dir(version: str | None = None) -> Path:
    """Directory for agent outputs: notes/traces/<version>/outputs/"""
    return traces_path() / (version or AGENT_VERSION) / "outputs"


def trace_logs_dir(version: str | None = None) -> Path:
    """Directory for reasoning logs: notes/traces/<version>/logs/"""
    return traces_path() / (version or AGENT_VERSION) / "logs"


# -- Path utilities -----------------------------------------------------------


def extract_glob_dir(pattern: str) -> str:
    """Extract the directory prefix from a glob pattern.

    Strips everything from the first glob wildcard character onward,
    returning the longest non-glob directory prefix.

    Used by permission hooks to validate Glob tool calls where the
    agent puts the full path in the ``pattern`` parameter instead of
    using the separate ``path`` parameter.

    Examples:
        >>> extract_glob_dir("/tmp/foo/**/*.py")
        '/tmp/foo'
        >>> extract_glob_dir("**/*.py")
        ''
        >>> extract_glob_dir("/tmp/foo/bar")
        '/tmp/foo/bar'
    """
    for i, c in enumerate(pattern):
        if c in "*?[":
            return pattern[:i].rstrip("/")
    return pattern


def path_is_under(file_path: str | Path, allowed_dirs: list[Path]) -> bool:
    """Check if a file path is under one of the allowed directories.

    Used by permission hooks to enforce RW/RO access.

    Args:
        file_path: Path to check.
        allowed_dirs: List of allowed parent directories.

    Returns:
        True if the path is under one of the allowed directories.
    """
    try:
        path = Path(file_path).resolve()
    except (OSError, ValueError):
        return False

    for allowed in allowed_dirs:
        try:
            path.relative_to(allowed.resolve())
            return True
        except ValueError:
            continue
    return False
