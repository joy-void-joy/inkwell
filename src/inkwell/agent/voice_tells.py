"""The house voice's tells, as vocabulary and as the rows that re-read a draft.

Nothing the voice guidance names belongs to one output format. An inflated
copula, a cycled synonym, and a claim handed to unnamed experts are tells in a
textbook chapter, a blog post, and a conference talk alike. So the vocabulary
the guidance bans and the rows that measure its tells are declared here once,
and a format draws on `VOICE_TELL_CHECKS` beside whatever its own shape asks
for. A format that wants them adds one splat to its list; a format that does
not is unaffected by their existing.

The banned vocabulary is declared as the groups the guidance itself names, and
both readers come off that one declaration: `LLM_VOCABULARY` is what a row
measures, `BANNED_VOCABULARY_PROSE` is what a writer reads. Neither is written
out a second time, so the list a writer is given and the list a draft is
checked against cannot drift apart.

Two conventions the guidance's prose needs and a word list cannot carry. A
word banned only in one sense — `rich` figuratively, `powerful` as filler
praise — is carried as the bare word, because every finding prints the sentence
it came from and the sense is judged there. A bracketed alternation is carried
as the run every alternative shares where that run is itself the tell, and as
its expansions where it is not: `in today's` covers the whole of "In today's
[digital age] [world]", but a bare `reminder` would not be the tell that
`stark reminder` is.
"""

from pydantic import BaseModel, Field

from inkwell.agent.format_checks import (
    BannedVocabulary,
    ContractionRate,
    DeclaredCheck,
    FormulaicOpenings,
    LinkingPredicates,
    MarkdownArtifacts,
    ParagraphLengthVariance,
    ParticipialTails,
    PunctuationDensity,
    QuestionRate,
    RuleOfThree,
    SentenceLengthVariance,
    SentenceOpeners,
    ShortSentences,
    SynonymCycling,
    VagueAttribution,
)


class BannedGroup(BaseModel):
    """One part of speech the voice guidance bans, as it groups them."""

    label: str = Field(description="The group's name, as the guidance prints it")
    words: list[str] = Field(description="The words or phrases the group rules out")


BANNED_VOCABULARY = [
    BannedGroup(
        label="Nouns",
        words=[
            "delve",
            "delves",
            "realm",
            "realms",
            "tapestry",
            "tapestries",
            "landscape",
            "landscapes",
            "testament",
            "testaments",
            "cornerstone",
            "paradigm",
            "beacon",
            "catalyst",
            "facet",
            "facets",
            "interplay",
            "intricacies",
            "nuance",
            "nuances",
            "synergy",
            "endeavor",
            "endeavors",
            "journey",
            "quest",
            "roadmap",
            "toolkit",
            "symphony",
            "kaleidoscope",
            "tempest",
            "mosaic",
            "bedrock",
            "linchpin",
            "nexus",
            "crucible",
            "fulcrum",
            "underpinning",
            "groundwork",
            "scaffold",
            "blueprint",
            "insight",
            "insights",
            "innovation",
            "resilience",
            "significance",
            "complexity",
            "complexities",
            "dynamics",
            "embodiment",
            "enlightenment",
            "exploration",
            "illumination",
            "imperative",
            "inspiration",
            "manifold",
            "poignancy",
            "resonance",
            "seamlessness",
            "timelessness",
            "transcendence",
            "versatility",
            "whimsy",
            "elusiveness",
            "relentlessness",
            "meticulousness",
        ],
    ),
    BannedGroup(
        label="Verbs",
        words=[
            "delving",
            "embark",
            "embarking",
            "leverage",
            "leveraging",
            "harness",
            "harnessing",
            "unlock",
            "unlocking",
            "unveil",
            "unveiling",
            "utilize",
            "facilitate",
            "navigate",
            "navigating",
            "cultivate",
            "foster",
            "fostering",
            "elevate",
            "elevating",
            "optimize",
            "underscore",
            "underscoring",
            "illuminate",
            "illuminating",
            "elucidate",
            "elucidating",
            "spearhead",
            "catalyze",
            "galvanize",
            "exemplify",
            "exemplifying",
            "embody",
            "embodying",
            "transcend",
            "transcending",
            "unravel",
            "unraveling",
            "reimagine",
            "reimagining",
            "revolutionize",
            "revolutionizing",
            "reverberate",
            "reverberating",
            "resonate",
            "resonating",
            "showcase",
            "showcasing",
            "grapple",
            "grappling",
            "entwine",
            "entwining",
            "intertwine",
            "intertwining",
            "partake",
            "emulate",
            "espouse",
            "evoke",
            "exacerbate",
            "craft",
            "crafted",
            "curate",
            "curated",
            "deepen",
            "deepening",
            "enhance",
            "enhancing",
            "ensure",
            "ensuring",
            "evolve",
            "evolving",
            "highlight",
            "highlighting",
            "inspire",
            "inspiring",
            "integrate",
            "integrating",
            "pivot",
            "pivoting",
            "strive",
            "striving",
            "weave",
            "weaving",
            "capture",
            "capturing",
            "empower",
            "empowering",
            "unpack",
        ],
    ),
    BannedGroup(
        label="Adjectives",
        words=[
            "robust",
            "innovative",
            "transformative",
            "comprehensive",
            "multifaceted",
            "pivotal",
            "seamless",
            "dynamic",
            "vibrant",
            "profound",
            "groundbreaking",
            "cutting-edge",
            "revolutionary",
            "unparalleled",
            "unprecedented",
            "holistic",
            "ever-evolving",
            "synergistic",
            "game-changing",
            "best-in-class",
            "state-of-the-art",
            "thought-provoking",
            "awe-inspiring",
            "crucial",
            "essential",
            "invaluable",
            "meticulous",
            "notable",
            "nuanced",
            "compelling",
            "indelible",
            "exemplary",
            "commendable",
            "authentic",
            "whimsical",
            "elegant",
            "grand",
            "potent",
            "vital",
            "significant",
            "rich",
            "sustainable",
            "powerful",
            "myriad",
            "plethora",
        ],
    ),
    BannedGroup(
        label="Adverbs",
        words=[
            "moreover",
            "furthermore",
            "additionally",
            "notably",
            "crucially",
            "importantly",
            "significantly",
            "profoundly",
            "meticulously",
            "seamlessly",
            "intricately",
            "relentlessly",
            "tirelessly",
            "indelibly",
            "pivotally",
            "poignantly",
            "vibrantly",
            "vividly",
            "aptly",
            "dynamically",
        ],
    ),
    BannedGroup(
        label="Phrases",
        words=[
            "in today's",
            "with the rapid advancement of",
            "in recent years we have seen",
            "it's important to note",
            "it is important to note",
            "it's worth noting",
            "it is worth noting",
            "it bears mentioning",
            "let's dive in",
            "let's delve into",
            "deep dive",
            "dive deep",
            "in conclusion",
            "in summary",
            "overall",
            "this is not an exhaustive list",
            "at the end of the day",
            "that being said",
            "in many ways",
            "harness the power of",
            "game changer",
            "navigate the landscape",
            "unlock the potential",
            "unlock potential",
            "unlock insights",
            "unlock value",
            "a testament to",
            "seamless integration",
            "drive innovation",
            "empower users",
            "revolutionize the industry",
            "paving the way for",
            "shed light on",
            "shed new light",
            "not only",
            "it's not about",
            "it is not about",
            "it's not just",
            "it is not just",
            "despite these challenges",
            "despite its",
            "stark reminder",
            "important reminder",
            "timely reminder",
            "powerful reminder",
            "serves as a",
            "stands as a",
            "plays a vital role",
            "a complex issue",
            "a complex challenge",
            "a complex landscape",
            "the rise of",
            "in a world where",
            "when it comes to",
            "valuable insights into",
            "significant impact",
            "significant role",
            "significant implications",
            "highlights the importance of",
            "a deeper understanding of",
            "simple yet",
        ],
    ),
]
"""The voice guidance's banned-vocabulary section, group by group as it states
it. The one place either reader reads from."""

LLM_VOCABULARY = list(
    dict.fromkeys(word for group in BANNED_VOCABULARY for word in group.words)
)
"""The vocabulary the voice guidance bans outright, flattened for the row that
measures it and deduplicated because a word the guidance lists under two parts
of speech is still one banned word. The default for a format's row, which takes
an override: which phrases read as filler is a judgement about register, and a
house with different tells declares its own."""

BANNED_VOCABULARY_PROSE = "\n".join(
    f"- **{group.label}** — {', '.join(group.words)}" for group in BANNED_VOCABULARY
)
"""The same list as the writer reads it before drafting, rendered from the
groups the row measures rather than restated beside them."""


VOICE_TELL_CHECKS: list[DeclaredCheck] = [
    BannedVocabulary(
        name="llm vocabulary",
        rule="None of the banned words or phrases appear — prefer the specific "
        "word to the impressive one.",
        phrases=LLM_VOCABULARY,
    ),
    LinkingPredicates(
        name="inflated predicates",
        rule="Write what things do, not what they are, and never reach for the "
        "longer word that means the same — `is` over `serves as`, `has` over "
        "`boasts`, `use` over `utilize`.",
        share_ceiling=0.05,
    ),
    ParticipialTails(
        name="participial tails",
        rule="No sentence ends on a comma and a gerund.",
        share_ceiling=0.05,
    ),
    SynonymCycling(
        name="synonym cycling",
        rule="Say the word again rather than reaching for a synonym — if the "
        "subject is a researcher, they stay the researcher.",
    ),
    RuleOfThree(
        name="rule of three",
        rule="The number of things in a list is the number the content has, "
        "not three because three sounds finished.",
    ),
    VagueAttribution(
        name="vague attribution",
        rule="An appeal to studies, experts, or scholars names them; where it "
        "cannot, the claim is found a source or dropped.",
    ),
    FormulaicOpenings(
        name="paragraph openings",
        rule="No run of paragraphs opens the same way.",
        repeat_ceiling=1,
    ),
    SentenceLengthVariance(
        name="sentence rhythm",
        rule="Sentence lengths vary; some sentences are four words.",
        spread_floor=5.0,
    ),
    ParagraphLengthVariance(
        name="paragraph rhythm",
        rule="Paragraph lengths vary rather than coming out uniform.",
        spread_floor=15.0,
    ),
    PunctuationDensity(
        name="em-dash density",
        rule="Em dashes are rationed to a genuine break in thought, one or two "
        "a section.",
        marks=["—"],
        per_thousand_ceiling=3.0,
    ),
    MarkdownArtifacts(
        name="markdown artifacts",
        rule="No markdown left showing — every marker either renders or is cut.",
    ),
    PunctuationDensity(
        name="semicolon density",
        rule="Semicolons are normal punctuation; how often the draft reaches "
        "for one is reported back as a number, never as a fault.",
        marks=[";"],
    ),
    PunctuationDensity(
        name="parenthesis density",
        rule="Parentheses are normal punctuation (and a subordinate thought "
        "belongs in them); the count is reported, not judged.",
        marks=["("],
    ),
    ContractionRate(
        name="contractions",
        rule="Contract the way a person talking would; the rate is reported "
        "back, because prose that never contracts reads as machine-written.",
    ),
    SentenceOpeners(
        name="conjunction openings",
        rule="A sentence may open on And or But where it flows; how often one "
        "does is reported, not judged.",
    ),
    QuestionRate(
        name="questions",
        rule="A question may pull the reader forward; how often the draft asks "
        "one is reported, not judged.",
    ),
    ShortSentences(
        name="short sentences",
        rule="Fragments and one-line emphasis are welcome; the share of short "
        "sentences is reported, not judged.",
    ),
]
"""Every row the house voice asks for, in the order a report reads best.

The verdict rows come first and the measurements last, because a reader
scanning for something to act on should not have to step over numbers that
hold nothing against the draft. A format splices this list into its own."""
