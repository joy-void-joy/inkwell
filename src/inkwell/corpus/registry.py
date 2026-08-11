"""What the corpus tracks, declared once per source.

This module is data. A source says where it publishes, how to enumerate it, who
publishes it, and how much weight a citation of it carries — and that is the
whole of adding a source. The walks themselves live in ``discovery``, so a new
entry here is a few lines rather than a module.

Declaring venue and authority here is what lets a citation *derive* its
authority instead of asserting one per claim: the writer already knows that a
document under ``anthropic`` is first-party from the lab it describes, and that
one under ``aisi`` is a government institute, because the source said so once.

``active`` is honesty, not a bug. A source can be worth tracking before its
enumeration is proven, and marking it inactive says "declared, not yet swept"
where the alternative is either a silent omission or a run that fails on it
every time. Every source the ported research database covered appears below —
either declared, or in ``DROPPED_SOURCES`` with the reason it is not a corpus
source at all.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from inkwell.corpus.discovery import (
    Avenue,
    ListingAvenue,
    SitemapAvenue,
    SweepAvenue,
    TableAvenue,
)
from inkwell.corpus.quality import DEFAULT_RULES, QualityRule
from inkwell.corpus.tags import (
    DEFAULT_VOCABULARY,
    EVALUATIONS,
    GOVERNANCE,
    MODEL_RELEASE,
    SYSTEM_CARD,
    TagTerm,
    TagVocabulary,
    organization_term,
)

type Authority = Literal["first-party", "government", "institute", "independent"]
"""What kind of standing a source's documents have.

- ``first-party`` — the organization describing its own work, which is the
  strongest evidence of what it did and the weakest of whether that was wise.
- ``government`` — a public body publishing in an official capacity.
- ``institute`` — a research organization publishing about others' work.
- ``independent`` — an individual or unaffiliated venue.
"""


class SourceDeclaration(BaseModel):
    """One tracked source: where it publishes, and what its word is worth."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Stable short name; also its directory in the corpus")
    display_name: str = Field(description="How the source is named to a reader")
    organization: str = Field(description="Who publishes it")
    venue: str = Field(description="The publication venue a citation should name")
    authority: Authority = Field(description="What kind of standing its word has")
    hosts: tuple[str, ...] = Field(
        default=(), description="Hosts whose documents belong to this source"
    )
    avenues: tuple[Avenue, ...] = Field(
        default=(), description="The publishing surfaces to enumerate"
    )
    tags: tuple[TagTerm, ...] = Field(
        default=(),
        description=(
            "What is true of every document this source publishes. Declared "
            "here for the same reason venue and authority are: a per-source "
            "fact belongs to the source, not to each document restating it"
        ),
    )
    active: bool = Field(
        default=True,
        description="Whether a run sweeps this source, or only tracks that it exists",
    )
    renders_with_javascript: bool = Field(
        default=False,
        description="Whether its pages need a real browser to produce content",
    )
    quality_rules: tuple[QualityRule, ...] = Field(
        default=DEFAULT_RULES,
        description="The heuristic this source's captures are judged against",
    )
    notes: str = Field(
        default="", description="What a reader should know before trusting the above"
    )

    def owns(self, host: str) -> bool:
        """Whether a host's documents belong to this source."""
        return any(host == known or host.endswith(f".{known}") for known in self.hosts)

    def organization_tag(self) -> TagTerm:
        """The tag every document of this source carries for who published it."""
        return organization_term(self.key, self.organization)

    def tags_for(self, category: str) -> tuple[str, ...]:
        """Every tag this declaration settles for a document in ``category``.

        Derived rather than judged, which is what keeps a judgement spending
        itself on the one thing it can answer that a declaration cannot: what
        this particular document is about.
        """
        avenues = (
            term.tag
            for avenue in self.avenues
            if avenue.category == category
            for term in avenue.tags
        )
        declared = (term.tag for term in self.tags)
        return tuple(sorted({self.organization_tag().tag, *declared, *avenues}))


class DroppedSource(BaseModel):
    """A source the ported database handled that this corpus deliberately omits.

    Recorded rather than left out, so "did we forget this" has an answer that is
    data instead of an absence.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    reason: str


ANTHROPIC_CDN = "www-cdn.anthropic.com"
"""Where Anthropic serves the system-card PDFs its own sitemap never lists."""


DECLARED_SOURCES: tuple[SourceDeclaration, ...] = (
    SourceDeclaration(
        key="aisi",
        display_name="UK AI Security Institute",
        organization="UK AI Security Institute",
        venue="AISI Research",
        authority="government",
        hosts=("aisi.gov.uk",),
        avenues=(
            SitemapAvenue(
                category="research", sitemap="https://www.aisi.gov.uk/sitemap.xml"
            ),
            SitemapAvenue(
                category="blog", sitemap="https://www.aisi.gov.uk/sitemap.xml"
            ),
        ),
        notes=(
            "Webflow, but it ships a real sitemap so no listing walk is needed. "
            "Research pages are landing pages that usually link the paper out to "
            "arXiv or a hosted PDF; the landing page is what gets stored, links "
            "intact. Renamed from AI Safety Institute — the key stays 'aisi'."
        ),
    ),
    SourceDeclaration(
        key="anthropic",
        display_name="Anthropic",
        organization="Anthropic",
        venue="Anthropic Research",
        authority="first-party",
        hosts=("anthropic.com",),
        avenues=(
            SitemapAvenue(
                category="research", sitemap="https://www.anthropic.com/sitemap.xml"
            ),
            TableAvenue(
                category="system-cards",
                tags=(SYSTEM_CARD, MODEL_RELEASE),
                listing="https://www.anthropic.com/system-cards",
                link_hosts=("anthropic.com",),
                link_suffixes=(".pdf",),
                link_contains=("-system-card",),
            ),
        ),
        notes=(
            "Two avenues under one source. The system cards are not in the "
            f"sitemap: newer ones are HTML pages, older ones are hash-named PDFs "
            f"on {ANTHROPIC_CDN}, and the listing table's first cell is the only "
            "readable title either way."
        ),
    ),
    SourceDeclaration(
        key="apollo",
        display_name="Apollo Research",
        organization="Apollo Research",
        venue="Apollo Research",
        authority="institute",
        hosts=("apolloresearch.ai",),
        avenues=(
            SitemapAvenue(
                category="science",
                sitemap="https://www.apolloresearch.ai/science-sitemap.xml",
            ),
            SitemapAvenue(
                category="governance",
                sitemap="https://www.apolloresearch.ai/governance-sitemap.xml",
            ),
            SitemapAvenue(
                category="blog",
                sitemap="https://www.apolloresearch.ai/post-sitemap.xml",
            ),
            SitemapAvenue(
                category="products",
                sitemap="https://www.apolloresearch.ai/products-sitemap.xml",
            ),
        ),
        notes=(
            "WordPress with Yoast, so each content type has its own sitemap. The "
            "page and taxonomy sitemaps are left out on purpose: static pages and "
            "tag archives carry no document of their own."
        ),
    ),
    SourceDeclaration(
        key="epoch",
        display_name="Epoch AI",
        organization="Epoch AI",
        venue="Epoch AI",
        authority="institute",
        hosts=("epoch.ai",),
        avenues=(
            SitemapAvenue(
                category="publications",
                sitemap="https://epoch.ai/sitemap-publications-0.xml",
            ),
            SitemapAvenue(
                category="data-insights",
                sitemap="https://epoch.ai/sitemap-data-insights-0.xml",
            ),
            SitemapAvenue(
                category="gradient-updates",
                sitemap="https://epoch.ai/sitemap-pages-0.xml",
            ),
            SitemapAvenue(
                category="epoch-after-hours",
                sitemap="https://epoch.ai/sitemap-pages-0.xml",
            ),
        ),
        notes=(
            "Four avenues over three sitemap files — the last two share one, which "
            "the page cache fetches once. /latest is an aggregated view of these "
            "and needs no avenue. The sitemaps carry no lastmod, so a run detects "
            "new documents from the index and edits from the content hash."
        ),
    ),
    SourceDeclaration(
        key="govai",
        display_name="GovAI (Centre for the Governance of AI)",
        organization="Centre for the Governance of AI",
        venue="GovAI",
        authority="institute",
        tags=(GOVERNANCE,),
        hosts=("governance.ai",),
        avenues=(
            ListingAvenue(
                category="research",
                listing="https://www.governance.ai/research",
                item_prefix="/research-paper/",
            ),
            ListingAvenue(
                category="analysis",
                listing="https://www.governance.ai/analysis",
                item_prefix="/analysis/",
            ),
            ListingAvenue(
                category="updates",
                listing="https://www.governance.ai/updates",
                item_prefix="/post/",
            ),
        ),
        notes=(
            "No sitemap at all, but the Webflow listings render server-side, so "
            "the items are in the initial HTML and no browser is needed. The "
            "listing path and the document path differ — /research lists "
            "/research-paper/<slug> — which is why each avenue declares both."
        ),
    ),
    SourceDeclaration(
        key="iaps",
        display_name="Institute for AI Policy and Strategy",
        organization="Institute for AI Policy and Strategy",
        venue="IAPS",
        authority="institute",
        tags=(GOVERNANCE,),
        hosts=("iaps.ai",),
        avenues=(
            SitemapAvenue(
                category="research", sitemap="https://www.iaps.ai/sitemap.xml"
            ),
            SitemapAvenue(category="news", sitemap="https://www.iaps.ai/sitemap.xml"),
        ),
        notes=(
            "Squarespace with one flat sitemap; team pages and the bare listings "
            "fall away because a document is a category plus exactly one segment."
        ),
    ),
    SourceDeclaration(
        key="metr",
        display_name="METR",
        organization="METR",
        venue="METR",
        authority="institute",
        tags=(EVALUATIONS,),
        hosts=("metr.org",),
        avenues=(
            SitemapAvenue(category="blog", sitemap="https://metr.org/sitemap.xml"),
            SitemapAvenue(category="notes", sitemap="https://metr.org/sitemap.xml"),
            SitemapAvenue(
                category="evaluations", sitemap="https://metr.org/sitemap.xml"
            ),
        ),
        notes=(
            "Static and server-rendered; the Substack fingerprint on the site is a "
            "subscribe embed, not a hosted blog. Slugs are mixed-case and redirect "
            "to lowercase, and are kept verbatim so the identity stays the one the "
            "sitemap gave. /research is a listing only, so it has no avenue, and "
            "the localized /es and /zh-Hans mirrors fall away on the leaf rule."
        ),
    ),
    SourceDeclaration(
        key="openai",
        display_name="OpenAI",
        organization="OpenAI",
        venue="OpenAI",
        authority="first-party",
        hosts=("openai.com",),
        avenues=(
            SweepAvenue(
                domains=(
                    "https://openai.com",
                    "https://deploymentsafety.openai.com",
                    "https://help.openai.com",
                ),
                apex="openai.com",
            ),
        ),
        active=False,
        notes=(
            "Inactive: the published surface is large enough that a first sweep "
            "should be a deliberate act rather than a side effect of syncing "
            "everything. The multi-domain shape is declared and proven; PDFs on "
            "cdn.openai.com need seeds once someone enumerates them."
        ),
    ),
    SourceDeclaration(
        key="deepmind",
        display_name="Google DeepMind",
        organization="Google DeepMind",
        venue="Google DeepMind",
        authority="first-party",
        hosts=("deepmind.google",),
        avenues=(
            SweepAvenue(domains=("https://deepmind.google",), apex="deepmind.google"),
        ),
        active=False,
        renders_with_javascript=True,
        notes=(
            "Inactive pending recon: the site is behind a challenge and renders "
            "client-side, so it needs the browser path, and its CDN PDFs need "
            "seeds. Declared with the JS flag set so activating it is one field."
        ),
    ),
    SourceDeclaration(
        key="mistral",
        display_name="Mistral AI",
        organization="Mistral AI",
        venue="Mistral AI",
        authority="first-party",
        hosts=("mistral.ai",),
        avenues=(SweepAvenue(domains=("https://mistral.ai",), apex="mistral.ai"),),
        active=False,
        notes=(
            "Inactive pending recon. Governance documents live on a separate CMS "
            "host and render client-side, so they will arrive as seeds."
        ),
    ),
    SourceDeclaration(
        key="xai",
        display_name="xAI",
        organization="xAI",
        venue="xAI",
        authority="first-party",
        hosts=("x.ai",),
        avenues=(SweepAvenue(domains=("https://x.ai",), apex="x.ai"),),
        active=False,
        renders_with_javascript=True,
        notes=(
            "Inactive pending recon: challenge-gated, and the documents are "
            "likely CDN PDFs a sitemap never lists."
        ),
    ),
    SourceDeclaration(
        key="meta",
        display_name="Meta AI",
        organization="Meta",
        venue="Meta AI",
        authority="first-party",
        hosts=("ai.meta.com",),
        avenues=(SweepAvenue(domains=("https://ai.meta.com",), apex="meta.com"),),
        active=False,
        notes="Inactive pending recon: the enumeration mechanism is unconfirmed.",
    ),
    SourceDeclaration(
        key="deepseek",
        display_name="DeepSeek",
        organization="DeepSeek",
        venue="DeepSeek",
        authority="first-party",
        hosts=("deepseek.com",),
        avenues=(
            SweepAvenue(
                domains=("https://deepseek.com", "https://api-docs.deepseek.com"),
                apex="deepseek.com",
            ),
        ),
        active=False,
        notes=(
            "Inactive pending recon, though likely the easiest of the group: the "
            "site appears static, so its sitemap should sweep as declared."
        ),
    ),
    SourceDeclaration(
        key="qwen",
        display_name="Alibaba Qwen",
        organization="Alibaba",
        venue="Qwen",
        authority="first-party",
        hosts=("qwen.ai", "qwenlm.github.io"),
        avenues=(
            SweepAvenue(
                domains=("https://qwenlm.github.io", "https://qwen.ai"), apex="qwen.ai"
            ),
        ),
        active=False,
        notes=(
            "Inactive pending recon. The GitHub Pages blog is static and would "
            "sweep cleanly; the product site is the unknown half."
        ),
    ),
    SourceDeclaration(
        key="moonshot",
        display_name="Moonshot AI (Kimi)",
        organization="Moonshot AI",
        venue="Moonshot AI",
        authority="first-party",
        hosts=("moonshot.ai",),
        avenues=(
            SweepAvenue(domains=("https://www.moonshot.ai",), apex="moonshot.ai"),
        ),
        active=False,
        notes=(
            "Inactive pending recon: the organization publishes across several "
            "domains and which one is canonical is not settled."
        ),
    ),
    SourceDeclaration(
        key="zhipu",
        display_name="Zhipu AI (GLM)",
        organization="Zhipu AI",
        venue="Zhipu AI",
        authority="first-party",
        hosts=("z.ai",),
        avenues=(SweepAvenue(domains=("https://z.ai",), apex="z.ai"),),
        active=False,
        notes=(
            "Inactive pending recon: as with Moonshot, the canonical publishing "
            "domain is not settled."
        ),
    ),
)
"""Every source the ported research database tracked, declared once each.

The seven with per-category avenues are the ones whose enumeration was proven
there; the nine swept lab sources were declared but never wired, and stay
inactive with the reason in their notes rather than disappearing.
"""


DROPPED_SOURCES: tuple[DroppedSource, ...] = (
    DroppedSource(
        key="arxiv",
        reason=(
            "Not a publisher to enumerate but an index reached per paper, and "
            "this project already has arXiv search and fetch tools."
        ),
    ),
    DroppedSource(
        key="wikipedia",
        reason=(
            "Reached per article rather than enumerated, and already covered by "
            "this project's Wikipedia tools."
        ),
    ),
    DroppedSource(
        key="substack",
        reason=(
            "A hosting platform rather than a source: the thing worth tracking is "
            "a particular publication, which would be declared under its own key."
        ),
    ),
    DroppedSource(
        key="alignment_forum",
        reason=(
            "A forum whose value is a thread and its comments, which the corpus "
            "layout does not model; the fetch tools read one post on demand."
        ),
    ),
    DroppedSource(
        key="pdf",
        reason=(
            "A fetch strategy, not a source. Every declaration here stores a PDF "
            "as a PDF, so it needs no source of its own."
        ),
    ),
)
"""What the ported database handled that is deliberately not a corpus source.

Its scrapers covered these; none is a publisher whose output can be enumerated,
which is what a corpus source has to be.
"""


def corpus_vocabulary(
    declarations: tuple[SourceDeclaration, ...] = DECLARED_SOURCES,
    base: TagVocabulary = DEFAULT_VOCABULARY,
) -> TagVocabulary:
    """The declared vocabulary, with one organization tag per declared source.

    The organization facet is derived here rather than written out beside the
    subjects, so declaring a source is the whole of adding its organization to
    what a browse can filter on — the same declaration that already says where
    it publishes and what its word is worth.
    """
    return base.model_copy(
        update={
            "organizations": tuple(entry.organization_tag() for entry in declarations)
        }
    )


def declaration_for(key: str) -> SourceDeclaration | None:
    """The declaration under ``key``, or None when nothing declares it."""
    return next((entry for entry in DECLARED_SOURCES if entry.key == key), None)


def active_declarations() -> tuple[SourceDeclaration, ...]:
    """The sources a bulk run sweeps: declared, active, and with an avenue."""
    return tuple(entry for entry in DECLARED_SOURCES if entry.active and entry.avenues)


def source_owning(host: str) -> SourceDeclaration | None:
    """Which declared source publishes on ``host``, where one does."""
    return next((entry for entry in DECLARED_SOURCES if entry.owns(host)), None)
