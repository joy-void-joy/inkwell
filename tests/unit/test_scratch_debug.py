# claude: ignore
"""Temporary diagnostic."""

from inkwell.agent.prose import MARKDOWN, markdown_blocks
from inkwell.agent.segmenter import reader


def test_children() -> None:
    draft = "**Attention is expensive.** The cost grows with sequence length."
    for token in MARKDOWN.parse(draft):
        print("TOP", token.type)
        for child in token.children or []:
            print("   CHILD", child.type, repr(child.content))


def test_debug() -> None:
    draft = "**Attention is expensive.** The cost grows with sequence length."
    for block in markdown_blocks(draft):
        print("KIND", repr(block.kind))
        print("TEXT", repr(block.text))
        print("PLAIN", repr(block.plain))
        print("BOLD", repr(block.bold_opening))
    prose = reader().read(draft)
    for p in prose.paragraphs:
        print("BOLDED?", p.summary_is_bolded)
        print("FIRST", repr(p.sentences[0].text), p.sentences[0].words)
        print("BOLDWORDS", reader().phrase(p.block.bold_opening))
