"""File-backed shared knowledge base for the writing pipeline.

All pipeline components (Comment Watcher, Orchestrator, stage agents)
read and write through PipelineNotes. Organized by category:

  notes/
  ├── artifacts/      # Typed pipeline artifacts (plan, research, voice)
  ├── comments/       # Classified author comments
  ├── drafts/         # Section drafts, merged article, final output
  ├── feedback/       # Rendered feedback files for stage consumption
  ├── research/       # Research notes triggered by comments
  └── terminal/       # Terminal input from the author

Thread-safe via asyncio.Lock. File-backed so background agents
can read without shared memory coordination.
"""

import asyncio
import json
import logging
from pathlib import Path

from pydantic import BaseModel

from inkwell.agent.models import ClassifiedComment

logger = logging.getLogger(__name__)


def max_file_index(directory: Path) -> int:
    """Return the highest numeric stem among JSON files in a directory (0 if empty)."""
    highest = 0
    if directory.exists():
        for path in directory.glob("*.json"):
            try:
                highest = max(highest, int(path.stem))
            except ValueError:
                pass
    return highest


class PipelineNotes:
    """File-backed shared notes for the pipeline.

    All agents read/write through this. The notes directory is created
    under the session's notes path.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.artifacts_dir = base_dir / "artifacts"
        self.comments_dir = base_dir / "comments"
        self.research_dir = base_dir / "research"
        self.terminal_dir = base_dir / "terminal"
        self.drafts_dir = base_dir / "drafts"
        self.feedback_dir = base_dir / "feedback"
        self.processed_dir = base_dir / "processed"
        self.lock = asyncio.Lock()

        for d in (
            self.artifacts_dir,
            self.comments_dir,
            self.drafts_dir,
            self.feedback_dir,
            self.research_dir,
            self.terminal_dir,
            self.processed_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.comment_counter = max_file_index(self.comments_dir)
        self.terminal_counter = max_file_index(self.terminal_dir)

    def draft_path(self, slug: str) -> Path:
        """Return path for a draft file: ``drafts/{slug}.md``."""
        return self.drafts_dir / f"{slug}.md"

    async def add_comment(self, classified: ClassifiedComment) -> None:
        """Write a classified comment to notes/comments/."""
        async with self.lock:
            self.comment_counter += 1
            path = self.comments_dir / f"{self.comment_counter:04d}.json"
            path.write_text(classified.model_dump_json(indent=2), encoding="utf-8")

    async def add_terminal_input(self, text: str) -> None:
        """Write terminal input as a clarification-level note."""
        async with self.lock:
            self.terminal_counter += 1
            comment = ClassifiedComment(
                comment_id=f"terminal-{self.terminal_counter}",
                content=text,
                impact="stage_local",
                tags=["terminal"],
            )
            path = self.terminal_dir / f"{self.terminal_counter:04d}.json"
            path.write_text(comment.model_dump_json(indent=2), encoding="utf-8")

    async def add_research_note(self, key: str, content: str) -> None:
        """Write a research note to notes/research/."""
        async with self.lock:
            path = self.research_dir / f"{key}.md"
            path.write_text(content, encoding="utf-8")

    async def list_comments(
        self, impact: str | None = None
    ) -> list[ClassifiedComment]:
        """Read all comments, optionally filtered by impact level."""
        async with self.lock:
            return self._read_comments(self.comments_dir, impact)

    async def list_terminal_inputs(self) -> list[ClassifiedComment]:
        """Read all terminal inputs."""
        async with self.lock:
            return self._read_comments(self.terminal_dir, None)

    async def has_plan_breaking(self) -> bool:
        """Check if any plan-breaking comments exist."""
        comments = await self.list_comments(impact="plan_breaking")
        return len(comments) > 0

    async def clear_plan_breaking(self) -> None:
        """Move plan-breaking comments to processed/ so they don't re-trigger."""
        async with self.lock:
            for path in sorted(self.comments_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    comment = ClassifiedComment.model_validate(data)
                    if comment.impact == "plan_breaking":
                        dest = self.processed_dir / path.name
                        path.rename(dest)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Failed to read note during clear: %s", path)

    async def get_all_feedback(self) -> str:
        """Render all accumulated feedback as markdown for prompt injection."""
        comments = await self.list_comments()
        terminal = await self.list_terminal_inputs()
        research_notes = await self._read_research_notes()

        if not comments and not terminal and not research_notes:
            return ""

        parts: list[str] = []

        plan_breaking = [c for c in comments if c.impact == "plan_breaking"]
        stage_local = [c for c in comments if c.impact == "stage_local"]
        clarifications = [c for c in comments if c.impact == "clarification"]

        if plan_breaking:
            parts.append("### Critical Author Feedback")
            for c in plan_breaking:
                line = f"- {c.content}"
                if c.anchor_text:
                    line += f' (on: "{c.anchor_text}")'
                if c.reply:
                    line += f" — Author reply: {c.reply}"
                parts.append(line)

        if stage_local:
            parts.append("\n### Author Direction")
            for c in stage_local:
                line = f"- {c.content}"
                if c.anchor_text:
                    line += f' (on: "{c.anchor_text}")'
                if c.reply:
                    line += f" — Author reply: {c.reply}"
                parts.append(line)

        if clarifications:
            parts.append("\n### Clarifications")
            for c in clarifications:
                line = f"- {c.content}"
                if c.reply:
                    line += f" — {c.reply}"
                parts.append(line)

        if terminal:
            parts.append("\n### Terminal Input")
            for t in terminal:
                parts.append(f"- {t.content}")

        if research_notes:
            parts.append("\n### Research Notes (from feedback)")
            for key, content in research_notes:
                parts.append(f"#### {key}\n{content}")

        return "\n\n## Author Feedback\n\n" + "\n".join(parts) + "\n"

    async def get_section_feedback(self, section: str) -> str:
        """Render feedback relevant to a specific section."""
        comments = await self.list_comments()
        terminal = await self.list_terminal_inputs()

        relevant = [
            c for c in comments
            if section.lower() in c.anchor_text.lower()
            or section.lower() in c.content.lower()
            or any(section.lower() in tag.lower() for tag in c.tags)
        ]
        relevant.extend(terminal)

        if not relevant:
            return ""

        lines = [f"## Feedback for '{section}'", ""]
        for c in relevant:
            line = f"- [{c.impact}] {c.content}"
            if c.anchor_text:
                line += f' (on: "{c.anchor_text}")'
            if c.reply:
                line += f" — Author: {c.reply}"
            lines.append(line)

        return "\n".join(lines) + "\n"

    async def clear_downstream(self, from_stage: str) -> None:
        """Clear notes invalidated by a restart.

        Currently preserves all notes — comments from the author are
        always relevant. Only research notes produced by the pipeline
        (not from comments) would be cleared, but those live in the
        PipelineSnapshot, not here.
        """
        logger.info("Notes: clear_downstream from '%s' (currently a no-op)", from_stage)

    def _read_comments(
        self, directory: Path, impact: str | None
    ) -> list[ClassifiedComment]:
        """Read ClassifiedComment JSON files from a directory."""
        results: list[ClassifiedComment] = []
        if not directory.exists():
            return results
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                comment = ClassifiedComment.model_validate(data)
                if impact is None or comment.impact == impact:
                    results.append(comment)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Failed to read note: %s", path)
        return results

    async def _read_research_notes(self) -> list[tuple[str, str]]:
        """Read all research notes as (key, content) pairs."""
        async with self.lock:
            results: list[tuple[str, str]] = []
            if not self.research_dir.exists():
                return results
            for path in sorted(self.research_dir.glob("*.md")):
                content = path.read_text(encoding="utf-8")
                results.append((path.stem, content))
            return results

    # -- Typed artifact I/O -----------------------------------------------

    def artifact_path(self, name: str) -> Path:
        """Return the path for a named artifact (e.g. 'plan' -> artifacts/plan.json)."""
        return self.artifacts_dir / f"{name}.json"

    def save_artifact(self, name: str, model: BaseModel) -> Path:
        """Serialize a Pydantic model to artifacts/{name}.json. Returns the path."""
        path = self.artifact_path(name)
        path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load_artifact[T: BaseModel](self, name: str, model_type: type[T]) -> T | None:
        """Load and validate an artifact. Returns None if the file doesn't exist."""
        path = self.artifact_path(name)
        if not path.exists():
            return None
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))

    def save_text_artifact(self, name: str, content: str, ext: str = "md") -> Path:
        """Save a plain text artifact (conversation, draft, etc.)."""
        path = self.artifacts_dir / f"{name}.{ext}"
        path.write_text(content, encoding="utf-8")
        return path

    def text_artifact_path(self, name: str, ext: str = "md") -> Path:
        """Return the path for a text artifact."""
        return self.artifacts_dir / f"{name}.{ext}"

    async def render_feedback_file(self, stage: str, extra: list[str] | None = None) -> Path:
        """Render accumulated feedback to a file agents can Read.

        Writes feedback/{stage}.md with all accumulated comments,
        terminal input, and any extra strings. Returns the path.
        """
        content = await self.get_all_feedback()
        if extra:
            extra_text = "\n".join(f"- {e}" for e in extra)
            content += f"\n\n## Stage-Specific Notes\n\n{extra_text}\n"

        path = self.feedback_dir / f"{stage}.md"
        path.write_text(content or "(No feedback yet.)", encoding="utf-8")
        return path
