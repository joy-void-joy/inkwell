"""Score a capture against declared rules, so the number can be traced back.

A bare quality number tells a downstream reader nothing it can act on. Here the
heuristic is a *table of rules*: each one has an id, a sentence saying what it
looks for, the penalty it costs, and whether it is severe enough to call the
capture unusable. An assessment records which rule ids fired, so a score always
resolves to the rules that produced it.

Assessment is advisory and never decides whether to store: a thin or mojibake'd
capture is still worth keeping and flagging, because the alternative is a gap
nobody can see. Thresholds live on the rule instances, so a source that needs
different ones declares its own tuple rather than editing this module.
"""

from abc import ABC, abstractmethod
from itertools import groupby

from pydantic import BaseModel, ConfigDict, Field

INVISIBLE_CHARS = "".join(
    [
        *(chr(point) for point in range(0x200B, 0x2010)),
        *(chr(point) for point in range(0x202A, 0x202F)),
        chr(0x2060),
        chr(0xFEFF),
    ]
)
"""Zero-width, bidi, and joiner characters. They survive extraction invisibly
and then break every exact match a reader tries, so any residue is a flag.
Written as codepoints to keep the source itself free of literal invisibles."""

REPLACEMENT_CHAR = chr(0xFFFD)
"""What a decoder leaves where it could not make sense of the bytes."""

MOJIBAKE_LEADS = "ÂÃâ"
MOJIBAKE_FOLLOWS = "\xa0¿–—’“”€™"
"""UTF-8 read as latin-1 leaves one of these lead characters immediately before
a high continuation byte. The pair is the signature; either alone is ordinary."""

MAX_HEADING_DEPTH = 6
"""How many leading hashes Markdown still reads as a heading."""


def invisible_count(body: str) -> int:
    return sum(1 for char in body if char in INVISIBLE_CHARS)


def has_mojibake(body: str) -> bool:
    """Whether the text shows a decoding error rather than intended characters."""
    if REPLACEMENT_CHAR in body:
        return True
    return any(
        lead in MOJIBAKE_LEADS and follow in MOJIBAKE_FOLLOWS
        for lead, follow in zip(body, body[1:])
    )


def is_heading(line: str) -> bool:
    """Whether one line is a Markdown heading with actual text after it."""
    depth = next(
        (n for n in range(MAX_HEADING_DEPTH, 0, -1) if line.startswith("#" * n)), 0
    )
    rest = line[depth:]
    return bool(depth) and rest[:1] in (" ", "\t") and bool(rest.strip())


def is_table_row(line: str) -> bool:
    """Whether one line is a Markdown table row."""
    trimmed = line.strip()
    return len(trimmed) > 1 and trimmed.startswith("|") and trimmed.endswith("|")


SPACED_CAPS_RUN = 5
"""How many single capital letters in a row read as a broken PDF render —
``R E F E R E N C E S`` rather than prose."""


def has_spaced_caps(body: str) -> bool:
    """Whether the text contains a run of lone capitals, a PDF-render artifact."""
    run = 0
    for word in body.split():
        if len(word) == 1 and word.isalpha() and word.isupper():
            run += 1
            if run >= SPACED_CAPS_RUN:
                return True
        else:
            run = 0
    return False


class MathDelimiters(BaseModel):
    """How many display and inline math delimiters a body carries.

    Counted from runs of unescaped ``$``: a run of two is one display
    delimiter, and an odd run leaves one inline delimiter over. An odd total of
    either means a delimiter is missing, which renders as garbage.
    """

    model_config = ConfigDict(frozen=True)

    display: int = 0
    inline: int = 0

    def display_unbalanced(self) -> bool:
        return self.display % 2 == 1

    def inline_unbalanced(self) -> bool:
        return self.inline % 2 == 1


def math_delimiters(body: str) -> MathDelimiters:
    """Read the math delimiters out of a body, ignoring escaped dollars."""
    unescaped = (
        char
        for index, char in enumerate(body)
        if char != "$" or index == 0 or body[index - 1] != "\\"
    )
    runs = [len(list(group)) for char, group in groupby(unescaped) if char == "$"]
    return MathDelimiters(
        display=sum(length // 2 for length in runs),
        inline=sum(length % 2 for length in runs),
    )


class DocumentMetrics(BaseModel):
    """What one capture measures, before any rule judges it.

    Measuring and judging are separate so that the same numbers can be recorded
    beside the verdict: a reader who disagrees with a rule can still see what it
    saw.
    """

    model_config = ConfigDict(frozen=True)

    title: str = ""
    chars: int = 0
    words: int = 0
    links: int = 0
    headings: int = 0
    tables: int = 0
    invisibles: int = 0
    blank: bool = False
    mojibake: bool = False
    spaced_caps: bool = False
    math: MathDelimiters = MathDelimiters()


MARKDOWN_LINK_JOIN = "]("
"""Where a Markdown link's text meets its target. Counting these is a proxy for
counting links, and a close enough one: prose almost never spells it by hand."""


def measure(content: str, *, title: str = "") -> DocumentMetrics:
    """Measure one capture without judging it."""
    lines = content.splitlines()
    return DocumentMetrics(
        title=title,
        chars=len(content),
        words=len(content.split()),
        links=content.count(MARKDOWN_LINK_JOIN),
        headings=sum(1 for line in lines if is_heading(line)),
        tables=sum(1 for line in lines if is_table_row(line)),
        invisibles=invisible_count(content),
        blank=not content.strip(),
        mojibake=has_mojibake(content),
        spaced_caps=has_spaced_caps(content),
        math=math_delimiters(content),
    )


class QualityRule(BaseModel, ABC):
    """One thing that can be wrong with a capture, and what it costs.

    A rule carries its own thresholds, so overriding a threshold means
    declaring the rule with a different number rather than editing the check.

    A declared union rather than an injected capability: the base names the
    question and each rule answers it, so the heuristic grows by one class.
    Abstract without naming ``ABC``, since pydantic's metaclass is an
    ``ABCMeta`` and enforces ``fires`` already.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str = Field(description="Stable id recorded when this rule fires")
    description: str = Field(description="What the rule looks for, in a sentence")
    penalty: float = Field(default=0.15, description="What firing costs the score")
    critical: bool = Field(
        default=False,
        description="Whether firing means the capture is unusable as it stands",
    )

    @abstractmethod
    def fires(self, metrics: DocumentMetrics) -> bool:
        """Whether this rule applies to a capture with these metrics."""


class ThinRule(QualityRule):
    """Too short to be the article it claims to be — usually a JS shell."""

    rule_id: str = "thin"
    description: str = "The extract is shorter than a real article would be"
    critical: bool = True
    min_chars: int = Field(
        default=800, description="Below this many characters, the capture is thin"
    )

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.chars < self.min_chars


class BlankRule(QualityRule):
    """Bytes were captured, but nothing that reads as content."""

    rule_id: str = "structurally_empty"
    description: str = "The capture has length but no non-whitespace content"
    critical: bool = True

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.chars > 0 and metrics.blank


class MissingTitleRule(QualityRule):
    """Nothing a reader could cite the document by."""

    rule_id: str = "no_title"
    description: str = "No usable title was captured"
    placeholders: tuple[str, ...] = Field(
        default=("", "untitled"), description="Titles that count as absent"
    )

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.title.strip().lower() in self.placeholders


class InlineMathRule(QualityRule):
    """An odd number of inline math delimiters, so one is missing."""

    rule_id: str = "latex_unbalanced_inline"
    description: str = "Inline math delimiters do not pair up"

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.math.inline_unbalanced()


class DisplayMathRule(QualityRule):
    """An odd number of display math delimiters, so one is missing."""

    rule_id: str = "latex_unbalanced_display"
    description: str = "Display math delimiters do not pair up"

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.math.display_unbalanced()


class SpacedCapsRule(QualityRule):
    """Letters spaced apart, which is how a bad PDF render reads."""

    rule_id: str = "spaced_caps"
    description: str = "A run of lone capital letters suggests a broken render"

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.spaced_caps


class MojibakeRule(QualityRule):
    """Characters that show the bytes were decoded with the wrong encoding."""

    rule_id: str = "mojibake"
    description: str = "The text shows signs of a decoding error"

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.mojibake


class InvisibleCharsRule(QualityRule):
    """Zero-width or bidi characters that break exact matching silently."""

    rule_id: str = "invisible_chars"
    description: str = "Zero-width or bidi characters survived extraction"

    def fires(self, metrics: DocumentMetrics) -> bool:
        return metrics.invisibles > 0


DEFAULT_RULES: tuple[QualityRule, ...] = (
    ThinRule(),
    BlankRule(),
    MissingTitleRule(),
    InlineMathRule(),
    DisplayMathRule(),
    SpacedCapsRule(),
    MojibakeRule(),
    InvisibleCharsRule(),
)
"""The heuristic, as data. A source declaring its own tuple overrides it —
raising ``ThinRule(min_chars=...)`` for a publisher of short notes, say."""


class QualityReport(BaseModel):
    """What the rules made of one capture, and the numbers they read.

    ``fired`` is what makes ``score`` traceable: every point deducted names the
    rule that deducted it, so a consumer can weigh the rules it cares about
    rather than trusting the total.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool = True
    score: float = 1.0
    fired: tuple[str, ...] = ()
    metrics: DocumentMetrics = DocumentMetrics()
    assessed: bool = True
    """Whether the rules actually ran. False for a PDF, whose text is never
    extracted — so there is nothing here to measure, and a score of 1.0 means
    "not judged" rather than "judged and found clean"."""


def assess(
    content: str,
    *,
    title: str = "",
    rules: tuple[QualityRule, ...] = DEFAULT_RULES,
) -> QualityReport:
    """Judge one capture against ``rules``, recording which of them fired."""
    metrics = measure(content, title=title)
    fired = [rule for rule in rules if rule.fires(metrics)]
    deducted = sum(rule.penalty for rule in fired)
    return QualityReport(
        ok=not any(rule.critical for rule in fired),
        score=round(max(0.0, 1.0 - deducted), 2),
        fired=tuple(rule.rule_id for rule in fired),
        metrics=metrics,
    )
