# lup: ignore[constant-declaration]
# Every constant here is a marker Codex writes into an apply_patch envelope,
# so the values are the provider's and reading a different set would decode
# a format no runtime emits.
"""Codex apply_patch envelope parsing for the hook dispatcher.

Codex exposes the complete envelope as ``tool_input.command``. Decoding it
into per-file documents lets the canonical edit policy judge the same
before/after batch that the native patcher will apply.

Rendered verbatim into each plugin's ``hooks/runtime`` as
``codex_patch.py``, so it imports only the standard library.
"""

from collections.abc import Callable

BEGIN = "*** Begin Patch"
END = "*** End Patch"
ADD = "*** Add File: "
DELETE = "*** Delete File: "
UPDATE = "*** Update File: "
MOVE = "*** Move to: "
HUNK = "@@"
EOF = "*** End of File"


class PatchedFile:
    """One file's documents on either side of a patch, ready for decide_edit."""

    path: str
    before: str | None
    after: str | None
    path_exists: bool

    def __init__(
        self,
        path: str,
        before: str | None,
        after: str | None,
        *,
        path_exists: bool,
    ) -> None:
        self.path = path
        self.before = before
        self.after = after
        self.path_exists = path_exists

    def operation(self) -> str:
        """Which class of change this is, for the gates that judge by it.

        Derived rather than carried from the envelope's own verb because the
        three the format spells — add, update, delete — are already implied by
        what the decode produced: an envelope that removed a file has no
        after, and one that added a file names a path the tree does not hold.
        An update is the remainder, and it is a fragment patch rather than a
        whole-file replacement, so it is never an overwrite.
        """
        if self.after is None or self.after == "":
            return "delete"
        if not self.path_exists:
            return "create"
        return "modify"


class ChangeChunk:
    """One context-anchored update chunk from a file section."""

    context: str | None
    old_lines: list[str]
    new_lines: list[str]
    end_of_file: bool

    def __init__(self, context: str | None) -> None:
        self.context = context
        self.old_lines = list()
        self.new_lines = list()
        self.end_of_file = False


class Replacement:
    """A source span and the lines replacing it."""

    start: int
    old_count: int
    new_lines: list[str]

    def __init__(self, start: int, old_count: int, new_lines: list[str]) -> None:
        self.start = start
        self.old_count = old_count
        self.new_lines = new_lines


def locate(
    source: list[str],
    preimage: list[str],
    start: int,
    *,
    end_of_file: bool = False,
) -> int:
    """Find the hunk's preimage as a contiguous run at or after ``start``."""
    if not preimage:
        return start
    for index in range(start, len(source) - len(preimage) + 1):
        matches = source[index : index + len(preimage)] == preimage
        anchored = not end_of_file or index + len(preimage) == len(source)
        if matches and anchored:
            return index
    raise ValueError("patch context does not match the file on disk")


def apply_hunks(current: str, chunks: list[ChangeChunk]) -> str:
    """Replay parsed chunks with Codex's context and EOF anchoring semantics."""
    source = current.splitlines()
    replacements: list[Replacement] = list()
    cursor = 0
    for chunk in chunks:
        if chunk.context is not None:
            cursor = locate(source, [chunk.context], cursor) + 1
        if not chunk.old_lines:
            if chunk.new_lines:
                replacements.append(Replacement(len(source), 0, list(chunk.new_lines)))
            continue
        old_lines = chunk.old_lines
        new_lines = chunk.new_lines
        try:
            start = locate(
                source,
                old_lines,
                cursor,
                end_of_file=chunk.end_of_file,
            )
        except ValueError:
            if old_lines[-1:] != [""]:
                raise
            old_lines = old_lines[:-1]
            new_lines = new_lines[:-1] if new_lines[-1:] == [""] else new_lines
            start = locate(
                source,
                old_lines,
                cursor,
                end_of_file=chunk.end_of_file,
            )
        replacements.append(Replacement(start, len(old_lines), list(new_lines)))
        cursor = start + len(old_lines)
    updated = list(source)
    for replacement in reversed(replacements):
        updated[replacement.start : replacement.start + replacement.old_count] = (
            replacement.new_lines
        )
    return "\n".join(updated) + ("\n" if updated else "")


class PatchParser:
    """Stateful parser for one complete Codex patch envelope."""

    lines: list[str]
    index: int
    files: list[PatchedFile]
    read_document: Callable[[str], str | None]

    def __init__(
        self,
        text: str,
        read_document: Callable[[str], str | None],
    ) -> None:
        self.lines = text.strip().splitlines()
        self.index = 1
        self.files = list()
        self.read_document = read_document
        if not self.lines or self.lines[0].strip() != BEGIN:
            raise ValueError("patch envelope is missing its Begin Patch header")
        if len(self.lines) == 1 or self.lines[-1].strip() != END:
            raise ValueError("patch envelope is missing its End Patch footer")

    def path(self, marker: str) -> str:
        """Return the non-empty path carried by the current header."""
        header = self.lines[self.index].strip()
        path = header[len(marker) :].strip()
        if not path:
            raise ValueError(f"{marker.strip()} requires a path")
        return path

    def current_document(self, path: str) -> str | None:
        """Read a path after accounting for earlier sections in this envelope."""
        for change in reversed(self.files):
            if change.path == path:
                return change.after
        return self.read_document(path)

    def record(self, path: str, before: str | None, after: str | None) -> None:
        """Append one policy-ready file transition."""
        self.files.append(
            PatchedFile(path, before, after, path_exists=before is not None)
        )

    def parse_add(self) -> None:
        """Parse an Add File section, including an existing overwritten file."""
        path = self.path(ADD)
        self.index += 1
        body: list[str] = list()
        while self.index < len(self.lines) - 1:
            line = self.lines[self.index]
            if not line.startswith("+"):
                break
            body.append(line[1:])
            self.index += 1
        if not body:
            raise ValueError(f"Add File section for {path!r} has no added lines")
        before = self.current_document(path)
        self.record(path, before, "".join(f"{line}\n" for line in body))

    def parse_delete(self) -> None:
        """Parse a Delete File section and require its preimage to exist."""
        path = self.path(DELETE)
        before = self.current_document(path)
        if before is None:
            raise ValueError(f"patch deletes a file that does not exist: {path}")
        self.record(path, before, None)
        self.index += 1

    def parse_chunks(self) -> list[ChangeChunk]:
        """Parse update chunks up to the next file section."""
        chunks: list[ChangeChunk] = list()
        chunk: ChangeChunk | None = None
        changed = False
        while self.index < len(self.lines) - 1:
            line = self.lines[self.index]
            stripped = line.strip()
            if (
                stripped.startswith(ADD)
                or stripped.startswith(DELETE)
                or stripped.startswith(UPDATE)
                or stripped == END
            ):
                break
            if stripped == HUNK or stripped.startswith(f"{HUNK} "):
                if chunk is not None:
                    chunks.append(chunk)
                context = stripped[len(HUNK) :].strip()
                chunk = ChangeChunk(context or None)
                self.index += 1
                continue
            if stripped == EOF:
                if chunk is None or not (chunk.old_lines or chunk.new_lines):
                    raise ValueError("End of File marker has no update lines")
                chunk.end_of_file = True
                self.index += 1
                break
            if line[:1] not in (" ", "-", "+"):
                raise ValueError(f"unsupported patch line: {line!r}")
            if chunk is None:
                chunk = ChangeChunk(None)
            marker = line[0]
            content = line[1:]
            match marker:
                case " ":
                    chunk.old_lines.append(content)
                    chunk.new_lines.append(content)
                case "-":
                    chunk.old_lines.append(content)
                case "+":
                    chunk.new_lines.append(content)
            changed = True
            self.index += 1
        if chunk is not None:
            chunks.append(chunk)
        if chunks and not changed:
            raise ValueError("Update File section has no change lines")
        return chunks

    def parse_update(self) -> None:
        """Parse an update and expand a move into both touched file paths."""
        source = self.path(UPDATE)
        self.index += 1
        destination: str | None = None
        if self.lines[self.index].strip().startswith(MOVE):
            destination = self.path(MOVE)
            self.index += 1
        chunks = self.parse_chunks()
        if destination is None and not chunks:
            raise ValueError(f"Update File section for {source!r} is empty")
        before = self.current_document(source)
        if before is None:
            raise ValueError(f"patch updates a file that does not exist: {source}")
        after = apply_hunks(before, chunks) if chunks else before
        if destination is None:
            self.record(source, before, after)
            return
        if destination == source:
            raise ValueError("a Move to path must differ from its source")
        destination_before = self.current_document(destination)
        self.record(source, before, None)
        self.record(destination, destination_before, after)

    def parse(self) -> list[PatchedFile]:
        """Decode every file section or reject the envelope."""
        while self.index < len(self.lines) - 1:
            header = self.lines[self.index].strip()
            if not header:
                self.index += 1
                continue
            if header.startswith(ADD):
                self.parse_add()
                continue
            if header.startswith(DELETE):
                self.parse_delete()
                continue
            if header.startswith(UPDATE):
                self.parse_update()
                continue
            raise ValueError(f"unsupported patch section: {header!r}")
        if not self.files:
            raise ValueError("patch envelope declares no file changes")
        return self.files


def patched_files(
    text: str, read_document: Callable[[str], str | None]
) -> list[PatchedFile]:
    """Decode an apply_patch envelope into per-file before/after documents.

    ``read_document`` supplies a path's current text, or None when it does
    not exist; injecting it keeps the decode testable away from a disk.
    Anything the format does not account for raises, so an envelope this
    parser cannot vouch for reaches the caller's conservative branch
    rather than a decision made on a misread patch.
    """
    return PatchParser(text, read_document).parse()
