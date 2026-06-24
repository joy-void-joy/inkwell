"""Saving pasted text as an upload must behave like a real file upload.

The ``/upload-text`` endpoint turns text the author has in hand into a named
document under the upload dir — usable as a source or a reference without first
writing a local file. These tests pin the behavior that matters: the saved file
lands on disk with readable content, a missing or non-text name is forced to a
text extension, an edit replaces its file in place (stable name, no orphan
copies), and a ``replace_path`` pointing outside the upload dir can never delete
an arbitrary file.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from inkwell.environment.web.models import UploadTextRequest
from inkwell.environment.web.routes import sessions


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "uploads"
    monkeypatch.setattr(sessions, "UPLOAD_DIR", target)
    return target


def test_coerce_text_filename_forces_a_text_extension() -> None:
    assert sessions.coerce_text_filename("notes") == "notes.md"
    assert sessions.coerce_text_filename("notes.txt") == "notes.txt"
    assert sessions.coerce_text_filename("scan.pdf") == "scan.md"
    assert sessions.coerce_text_filename("  ") == "pasted.md"


async def test_upload_text_writes_readable_file(upload_dir: Path) -> None:
    result = await sessions.upload_text(
        UploadTextRequest(filename="notes", content="hello world")
    )
    saved = Path(result.path)
    assert saved.parent == upload_dir
    assert saved.name == "notes.md"
    assert saved.read_text(encoding="utf-8") == "hello world"
    assert result.size == len("hello world".encode())


async def test_upload_text_edit_replaces_in_place(upload_dir: Path) -> None:
    first = await sessions.upload_text(
        UploadTextRequest(filename="notes.md", content="v1")
    )
    second = await sessions.upload_text(
        UploadTextRequest(filename="notes.md", content="v2", replace_path=first.path)
    )
    assert second.filename == "notes.md"
    assert Path(second.path).read_text(encoding="utf-8") == "v2"
    assert [p.name for p in upload_dir.iterdir()] == ["notes.md"]


async def test_upload_text_without_replace_keeps_distinct_copies(
    upload_dir: Path,
) -> None:
    await sessions.upload_text(UploadTextRequest(filename="notes.md", content="a"))
    await sessions.upload_text(UploadTextRequest(filename="notes.md", content="b"))
    assert sorted(p.name for p in upload_dir.iterdir()) == ["notes.md", "notes_1.md"]


async def test_upload_text_replace_path_outside_upload_dir_is_ignored(
    upload_dir: Path, tmp_path: Path
) -> None:
    outsider = tmp_path / "secret.txt"
    outsider.write_text("do not delete", encoding="utf-8")
    await sessions.upload_text(
        UploadTextRequest(filename="x.md", content="hi", replace_path=str(outsider))
    )
    assert outsider.exists()


async def test_upload_text_rejects_oversize_content(
    upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(HTTPException) as excinfo:
        await sessions.upload_text(
            UploadTextRequest(filename="x.md", content="too long")
        )
    assert excinfo.value.status_code == 413
