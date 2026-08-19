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
from inkwell.corpus.registry import declaration_for
from inkwell.corpus.storage import CorpusStore, SourceShard, StoredDocument
from inkwell.corpus.tagging import RetagReport, RetagStep
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


async def announcing(
    declarations: tuple[object, ...],
    corpus: CorpusStore,
    *,
    force: bool,
    concurrency: int,
    progress: object,
) -> tuple[RetagReport, ...]:
    """A re-tag that judges nothing and reports two documents landing."""
    assert callable(progress)
    for done in (1, 2):
        progress(RetagStep(source="aisi", done=done, total=2))
    return (RetagReport(source="aisi", examined=2, retagged=2),)


def test_a_retag_says_where_it_has_got_to_where_no_bar_can_be_drawn(
    swept: CorpusStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Captured or piped, a redrawing bar is escape codes rather than a display.

    The failure this refuses is the one that made the command look broken:
    deciding a bar cannot be drawn and then saying nothing at all for as long
    as a few hundred delegated judgements take.
    """
    monkeypatch.setattr(commands.sys.stderr, "isatty", lambda: False)
    monkeypatch.setattr(commands, "retag_corpus", announcing)
    declaration = declaration_for("aisi")
    assert declaration is not None

    commands.judge_with(swept, (declaration,), force=False, concurrency=1)

    said = capsys.readouterr()
    assert "aisi 1/2" in said.err
    assert "aisi 2/2" in said.err
    assert "aisi: examined 2" in said.out


def test_a_retag_draws_a_bar_over_the_documents_it_has_to_judge(
    swept: CorpusStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sized in documents, because a source of two hundred is one tick otherwise."""
    monkeypatch.setattr(commands.sys.stderr, "isatty", lambda: True)

    bar = commands.judging_bar(2)

    assert bar is not None
    assert bar.total == 2
    bar.close()
