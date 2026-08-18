"""The operator commands, and the order they have to be run in.

What is worth testing here is the one thing the three-command split can get
wrong and nothing downstream would report: embedding a corpus before anything
has judged it. That does not produce a thinner semantic layer, it produces one
every PDF is absent from — and an absent document reads exactly like a corpus
that never held it, which is why the refusal is a command's behaviour rather
than a line of documentation.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

import inkwell.devtools.corpus as commands
from inkwell.corpus.storage import CorpusStore, SourceShard, StoredDocument
from inkwell.corpus.tags import DocumentTags


def stored(
    slug: str, *, kind: str = "markdown", judged: bool, abstract: str = ""
) -> StoredDocument:
    """One document as a sweep or a re-tag would have left it."""
    return StoredDocument(
        slug=slug,
        url=f"https://fixture.test/{slug}",
        title=slug,
        kind="pdf" if kind == "pdf" else "markdown",
        abstract=abstract,
        tags=DocumentTags(judged=judged),
    )


@pytest.fixture
def swept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CorpusStore:
    """A corpus one sweep has filled and no judgement has been near."""
    store = CorpusStore(root=tmp_path)
    store.save(
        SourceShard(
            source="aisi",
            documents=[
                stored("a-paper", kind="pdf", judged=False),
                stored("a-post", judged=False, abstract="Its opening prose."),
            ],
        )
    )
    monkeypatch.setattr(commands, "corpus_root", lambda: tmp_path)
    return store


def test_embedding_an_unjudged_corpus_is_refused_and_says_what_to_run(
    swept: CorpusStore,
) -> None:
    """Recoverable in one command, which a silently short layer is not."""
    result = CliRunner().invoke(commands.app, ["embed"])

    assert result.exit_code == 1
    assert "1 documents have nothing to embed until they are judged" in result.output
    assert "aisi: 1" in result.output
    assert "corpus retag" in result.output


def test_the_refusal_names_only_what_a_retag_would_rescue(
    swept: CorpusStore,
) -> None:
    """The post has an abstract, so no judgement is what stands between it and
    a vector — counting it would make the refusal unclearable."""
    result = CliRunner().invoke(commands.app, ["embed"])

    assert "aisi: 1" in result.output
    assert "a-post" not in result.output


def test_allowing_gaps_embeds_the_rest_instead_of_refusing(
    swept: CorpusStore,
) -> None:
    """An operator who knows the PDF cannot be judged is not held up by it."""
    result = CliRunner().invoke(commands.app, ["embed", "--allow-gaps"])

    assert "have nothing to embed" not in result.output
    assert "embedding with" in result.output


def test_a_judged_corpus_is_embedded_without_being_asked_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal clears itself: it is a state of the corpus, not a flag."""
    store = CorpusStore(root=tmp_path)
    store.save(
        SourceShard(
            source="aisi", documents=[stored("a-paper", kind="pdf", judged=True)]
        )
    )
    monkeypatch.setattr(commands, "corpus_root", lambda: tmp_path)

    result = CliRunner().invoke(commands.app, ["embed"])

    assert "have nothing to embed" not in result.output
