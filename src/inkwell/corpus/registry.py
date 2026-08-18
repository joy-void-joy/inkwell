"""What the corpus tracks, declared once per source.

This module is data. A source says where it publishes, how to enumerate it, who
publishes it, and how much weight a citation of it carries — and that is the
whole of adding a source. The walks themselves live in ``discovery``, so a new
entry here is a few lines rather than a module.

Declaring venue and authority here is what lets a citation *derive* its
authority instead of asserting one per claim: the writer already knows that a
document under ``anthropic`` is a lab publication and one under ``aisi`` an
official report, because the source said so once.

That standing is spelled in :data:`~inkwell.agent.provenance.Venue`, the same
vocabulary every other acquisition path is judged in. A list of its own here
would be a second table to keep in agreement with that one — what
``provenance``'s own docstring rules out — and the disagreement would surface
as a corpus document cited with a standing no other path could have given it.
Whether a source describes its own work or somebody else's is the other axis,
``EvidentialRole``, and belongs per citation rather than per source: a lab is
the primary source for its own results and commentary on everyone else's.

``active`` is honesty, not a bug. A source can be worth tracking before its
enumeration is proven, and marking it inactive says "declared, not yet swept"
where the alternative is either a silent omission or a run that fails on it
every time. Every source the ported research database covered appears below —
either declared, or in ``DROPPED_SOURCES`` with the reason it is not a corpus
source at all.
"""

from pydantic import BaseModel, ConfigDict, Field

from inkwell.agent.provenance import DomainVenue, Venue
from inkwell.corpus.discovery import (
    Avenue,
    FeedAvenue,
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
    SECURITY,
    SYSTEM_CARD,
    TagTerm,
    TagVocabulary,
    organization_term,
)


class SourceDeclaration(BaseModel):
    """One tracked source: where it publishes, and what its word is worth."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Stable short name; also its directory in the corpus")
    display_name: str = Field(description="How the source is named to a reader")
    organization: str = Field(description="Who publishes it")
    venue: str = Field(description="The publication venue a citation should name")
    authority: Venue = Field(
        description=(
            "What kind of thing this source publishes, in the same vocabulary "
            "every other acquisition path is judged in — so a citation derives "
            "its standing here instead of asserting one"
        )
    )
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
    needs_browser: bool = Field(
        default=False,
        description="Whether its pages need a real browser to reach their content "
        "— a client-rendered shell, or a host that refuses a plain fetcher",
    )
    thin_chars: int = Field(
        default=0,
        description="How short an extraction has to be before this source's pages "
        "are worth rendering, where the shared default reads a shell as an "
        "article; 0 takes the default. A shell's size is a fact about a site, "
        "so the source that has one says how big it is",
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

APOLLO_SITEMAP = "https://www.apolloresearch.ai/sitemap.xml"
"""The one sitemap Apollo publishes, which every one of its avenues walks.

Named once because four avenues share it: Webflow emits a single sitemap for
the whole site, so what separates science from governance is the path prefix
rather than the file, and four copies of this URL would be four places to
correct the next time the site moves.
"""


AI_HEADLINE_TERMS: tuple[str, ...] = (
    "ai ",
    " ai",
    "a.i.",
    "artificial intelligence",
    "machine learning",
    "llm",
    "chatbot",
    "openai",
    "anthropic",
    "claude",
    "chatgpt",
    "gemini",
    "deepmind",
    "hugging face",
    "gpt-",
    "copilot",
    "deepfake",
    "agentic",
)
"""What marks a general-desk headline as being about AI.

Used only where an outlet publishes no AI section of its own and the corpus
takes its security or technology feed instead. Padded forms for the bare
acronym, because an unpadded "ai" matches *said*, *maintain*, and *chain*.

A term list is a blunt instrument and this one is deliberately narrow: it is
the filter that decides what is never fetched, so a miss costs one article
while a false positive costs a fetch and a tagging pass. Where an outlet does
maintain an AI section, that section is the filter and this is not used.
"""


DECLARED_SOURCES: tuple[SourceDeclaration, ...] = (
    SourceDeclaration(
        key="aisi",
        display_name="UK AI Security Institute",
        organization="UK AI Security Institute",
        venue="AISI Research",
        authority="official_report",
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
        authority="lab_publication",
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
        authority="lab_publication",
        hosts=("apolloresearch.ai",),
        avenues=(
            SitemapAvenue(category="science", sitemap=APOLLO_SITEMAP),
            SitemapAvenue(category="governance", sitemap=APOLLO_SITEMAP),
            SitemapAvenue(category="blog", sitemap=APOLLO_SITEMAP),
            SitemapAvenue(category="monitoring", sitemap=APOLLO_SITEMAP),
        ),
        notes=(
            "Webflow, which publishes one sitemap for the whole site rather "
            "than one per content type, so every avenue walks the same file and "
            "the category is the path prefix that separates them. Press, team, "
            "and testimonials are left out on purpose: they carry announcements "
            "and profiles rather than documents of Apollo's own."
        ),
    ),
    SourceDeclaration(
        key="epoch",
        display_name="Epoch AI",
        organization="Epoch AI",
        venue="Epoch AI",
        authority="lab_publication",
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
        authority="lab_publication",
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
        authority="lab_publication",
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
        authority="lab_publication",
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
        authority="lab_publication",
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
        needs_browser=True,
        thin_chars=1500,
        notes=(
            "Swept across three domains: the main site, the deployment-safety "
            "site, and the help centre. The published surface is large, so the "
            "first sweep was taken as a deliberate act rather than inherited "
            "from a sync of everything — it is the primary account of the July "
            "2026 Hugging Face intrusion, which is what settled it. PDFs on "
            "cdn.openai.com still need seeds once someone enumerates them. "
            "openai.com refuses a plain client with a 403, and the "
            "deployment-safety site renders each section client-side from one "
            "shell, so both ways of being out of reach apply here and the "
            "browser path is what answers either. The shell clears the shared "
            "thin floor comfortably — it extracts to 1177 characters of "
            "introduction, where 800 is the default — so the floor is raised "
            "to sit above it and below the 2620 of the shortest page here that "
            "is genuinely an article."
        ),
    ),
    SourceDeclaration(
        key="deepmind",
        display_name="Google DeepMind",
        organization="Google DeepMind",
        venue="Google DeepMind",
        authority="lab_publication",
        hosts=("deepmind.google",),
        avenues=(
            SweepAvenue(domains=("https://deepmind.google",), apex="deepmind.google"),
        ),
        active=False,
        needs_browser=True,
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
        authority="lab_publication",
        hosts=("mistral.ai",),
        avenues=(SweepAvenue(domains=("https://mistral.ai",), apex="mistral.ai"),),
        active=False,
        notes=(
            "Inactive pending recon. Governance documents live on "
            "legal.cms.mistral.ai and render client-side, so they will arrive "
            "as seeds rather than through the sweep."
        ),
    ),
    SourceDeclaration(
        key="xai",
        display_name="xAI",
        organization="xAI",
        venue="xAI",
        authority="lab_publication",
        hosts=("x.ai",),
        avenues=(SweepAvenue(domains=("https://x.ai",), apex="x.ai"),),
        active=False,
        needs_browser=True,
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
        authority="lab_publication",
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
        authority="lab_publication",
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
        authority="lab_publication",
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
        authority="lab_publication",
        hosts=("moonshot.ai",),
        avenues=(
            SweepAvenue(domains=("https://www.moonshot.ai",), apex="moonshot.ai"),
        ),
        active=False,
        notes=(
            "Inactive pending recon: moonshot.ai, moonshot.cn, and kimi.com all "
            "carry the organization's writing and which is canonical is not "
            "settled. Recon is deciding that, not finding a sitemap."
        ),
    ),
    SourceDeclaration(
        key="zhipu",
        display_name="Zhipu AI (GLM)",
        organization="Zhipu AI",
        venue="Zhipu AI",
        authority="lab_publication",
        hosts=("z.ai",),
        avenues=(SweepAvenue(domains=("https://z.ai",), apex="z.ai"),),
        active=False,
        notes=(
            "Inactive pending recon: as with Moonshot, the canonical publishing "
            "domain is not settled — z.ai, zhipuai.cn, and bigmodel.cn are the "
            "three it is between."
        ),
    ),
    SourceDeclaration(
        key="techcrunch",
        display_name="TechCrunch",
        organization="TechCrunch",
        venue="TechCrunch",
        authority="news",
        hosts=("techcrunch.com",),
        avenues=(
            FeedAvenue(
                category="ai",
                feed="https://techcrunch.com/category/artificial-intelligence/feed/",
                apex="techcrunch.com",
            ),
        ),
        notes=(
            "Publishes its own AI section as a feed, so the section is the "
            "filter and no headline matching is needed. Feed verified: "
            "'AI News & Artificial Intelligence | TechCrunch', RSS 2.0."
        ),
    ),
    SourceDeclaration(
        key="bleepingcomputer",
        display_name="BleepingComputer",
        organization="BleepingComputer",
        venue="BleepingComputer",
        authority="news",
        hosts=("bleepingcomputer.com",),
        avenues=(
            FeedAvenue(
                category="security",
                tags=(SECURITY,),
                feed="https://www.bleepingcomputer.com/feed/",
                apex="bleepingcomputer.com",
                require_terms=AI_HEADLINE_TERMS,
            ),
        ),
        notes=(
            "A security desk with no AI section, so the whole feed is walked "
            "and headlines carry the topic filter. First to report the "
            "operational detail on the July 2026 Hugging Face intrusion, which "
            "is why it is declared. Its edge refuses a client claiming to be "
            "Chrome and serves one that says what it is, so it needs no "
            "browser and never did — what it needed was an honest user agent, "
            "and the 403 it answered for months was earned rather than "
            "arbitrary."
        ),
    ),
    SourceDeclaration(
        key="cyberscoop",
        display_name="CyberScoop",
        organization="CyberScoop",
        venue="CyberScoop",
        authority="news",
        hosts=("cyberscoop.com",),
        avenues=(
            FeedAvenue(
                category="security",
                tags=(SECURITY,),
                feed="https://cyberscoop.com/feed/",
                apex="cyberscoop.com",
                require_terms=AI_HEADLINE_TERMS,
            ),
        ),
        notes=(
            "Security desk, same shape as BleepingComputer. Feed verified: "
            "'CyberScoop', RSS 2.0."
        ),
    ),
    SourceDeclaration(
        key="bbc",
        display_name="BBC News",
        organization="BBC",
        venue="BBC News",
        authority="news",
        hosts=("bbc.co.uk", "bbc.com"),
        avenues=(
            FeedAvenue(
                category="technology",
                feed="https://feeds.bbci.co.uk/news/technology/rss.xml",
                apex="bbc.co.uk",
                require_terms=AI_HEADLINE_TERMS,
            ),
        ),
        notes=(
            "The technology desk, filtered on headlines: BBC publishes an AI "
            "topic page but no AI feed. Articles live on bbc.com while the "
            "feed is served from feeds.bbci.co.uk, which is why the apex is "
            "declared rather than taken from the feed's own host. Feed "
            "verified: 'BBC News', RSS 2.0."
        ),
    ),
)
"""Every source the corpus tracks, declared once each.

The seven with per-category avenues are the ones whose enumeration was proven
in the ported research database; the nine swept lab sources were declared but
never wired, and stay inactive with the reason in their notes rather than
disappearing. The news desks are the newest group, and the only one where the
corpus subsets a source rather than taking everything it publishes — a general
outlet's whole output is not what a writing pipeline wants standing behind it.
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


def corpus_host_venues() -> list[DomainVenue]:
    """Every declared source's hosts, as rows the venue derivation reads.

    This is how a corpus document gets a venue nobody asserted: the source
    already declares which hosts it publishes under and what kind of thing it
    publishes there, so handing those rows to :class:`VenueRules` lets the
    ordinary derivation answer for a corpus document exactly as it does for a
    fetched one. Composing rather than mapping is the point — there is one
    vocabulary and one lookup, not two tables to keep in agreement.
    """
    return [
        DomainVenue(domain=host, venue=declaration.authority)
        for declaration in DECLARED_SOURCES
        for host in declaration.hosts
    ]
