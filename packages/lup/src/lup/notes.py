"""RO/RW notes directory structure for agent sessions.

Key patterns:
1. Explicit separation of RW (session can write) and RO (historical, read-only)
2. Session-specific directories prevent cross-session pollution
3. Logs directory is NOT accessible to agent (for feedback loop only)
4. Permission hooks enforce the access control

Examples:
    Set up session directories and wire into permission hooks::

        >>> notes = setup_notes(session_id="12345", task_id="my-task")
        >>> notes.rw  # Agent can write here
        [PosixPath('.../sessions/12345'), PosixPath('.../outputs/my-task/...')]
        >>> notes.ro  # Agent can only read here
        [PosixPath('.../traces')]
"""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from lup.paths import outputs_dir, runtime_logs_path, sessions_dir, trace_logs_dir


class NotesConfig(BaseModel):
    """Notes folder configuration with explicit RW/RO separation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: Path = Field(description="This session's working directory")
    output: Path = Field(description="Where this session saves its outputs")
    trace_log: Path = Field(description="Trace log path (agent cannot access)")
    rw: list[Path] = Field(default_factory=list, description="Read-write directories")
    ro: list[Path] = Field(default_factory=list, description="Read-only directories")

    @property
    def all_dirs(self) -> list[Path]:
        """All directories the agent can access (RW + RO)."""
        return self.rw + self.ro


def setup_notes(
    session_id: str,
    task_id: str | None = None,
    type: str | None = None,
) -> NotesConfig:
    """Create session-specific notes folder structure.

    Uses version-aware paths from lup.paths. Separates:
    - RW directories: This session can write here
    - RO directories: Historical data, read-only for this session
    - Logs: Agent cannot access (for feedback loop analysis)

    Args:
        session_id: Unique session identifier.
        task_id: Optional task identifier (for organizing by task).
        type: Optional prefix inserted under sessions/, outputs/, and logs/
            to separate data by category (e.g. "background", "interactive").

    Returns:
        NotesConfig with RW and RO directories separated.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    sessions_base = sessions_dir() / type if type else sessions_dir()
    outputs_base = outputs_dir() / type if type else outputs_dir()
    logs_base = trace_logs_dir() / type if type else trace_logs_dir()

    session_path = sessions_base / session_id
    output_path = outputs_base / (task_id or session_id) / timestamp

    session_path.mkdir(parents=True, exist_ok=True)
    output_path.mkdir(parents=True, exist_ok=True)
    runtime_logs_path().mkdir(parents=True, exist_ok=True)

    trace_log = logs_base / session_id / f"{timestamp}.md"
    trace_log.parent.mkdir(parents=True, exist_ok=True)

    return NotesConfig(
        session=session_path,
        output=output_path,
        trace_log=trace_log,
        rw=[session_path, output_path],
        ro=[outputs_dir().parent],
    )
