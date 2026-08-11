"""The segmenter that fills the sentence seam, backed by syntok.

Sentence boundaries come from a real segmenter rather than from a rule about
which character precedes a space, so an abbreviation, a decimal, a quotation,
and a bracketed citation stop ending sentences that were never over. That is
what the length-variance rows need to mean anything.

syntok is pure Python, which is why it is the one here: the project floor is
Python 3.14 and spaCy publishes no wheel that interpreter can install without
a source build of Cython, thinc, and blis. `pyproject.toml` declares spaCy as
the `pos` extra so the seam records where a part-of-speech implementation drops
in — and the two rows that would read a parse, copula avoidance and participial
tails, ship as declared detectors over these tokens meanwhile. They fire on the
tells the author actually enumerated today, and become general the day a
`Segmenter` backed by a parser fills the same seam, with no change above it.
"""

from functools import cache

from syntok.segmenter import split
from syntok.tokenizer import Token, Tokenizer

from inkwell.agent.prose import ProseReader, Segmenter, Sentence


def carries_a_word(value: str) -> bool:
    """Whether a token carries a word rather than punctuation alone."""
    return any(character.isalnum() for character in value)


class SyntokSegmenter(Segmenter):
    """Segments prose into sentences and tokens with syntok.

    Fills the `Segmenter` seam; nothing holds one of these directly except the
    `ProseReader` built around it.
    """

    def __init__(self, tokenizer: Tokenizer | None = None) -> None:
        self.tokenizer = tokenizer or Tokenizer(replace_not_contraction=False)

    def sentences(self, text: str) -> list[Sentence]:
        """The sentences of one prose block, each as the tokens it is made of."""
        if not text.strip():
            return []
        return [
            self.describe(tokens) for tokens in split(self.tokenizer.tokenize(text))
        ]

    def describe(self, tokens: list[Token]) -> Sentence:
        """One segmented sentence as the token runs a declared row reads."""
        words = [token.value.lower() for token in tokens if carries_a_word(token.value)]
        return Sentence(
            text=Tokenizer.to_text(tokens).strip(),
            tokens=[token.value.lower() for token in tokens],
            words=words,
            opening=" ".join(words[:2]),
        )

    def words(self, text: str) -> list[str]:
        """A fragment's word forms, lowered, with punctuation dropped.

        A declared phrase and the sentence it is matched against both come
        through here, so the two are tokenized the same way and a banned
        `crucial` cannot fire on the inside of `crucially`.
        """
        return [
            token.value.lower()
            for token in self.tokenizer.tokenize(text)
            if carries_a_word(token.value)
        ]


@cache
def reader() -> ProseReader:
    """The shared reader, built once per process."""
    return ProseReader(SyntokSegmenter())
