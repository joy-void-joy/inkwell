"""The parts of a Google Docs document this project reads.

The Docs API answers with deeply nested JSON whose every level is optional,
which is why reading it by hand becomes a wall of ``isinstance`` guards and
defaulted ``.get`` calls that say nothing about the shape. Declaring the shape
once moves that narrowing into one ``model_validate`` and lets every reader
above use attribute access.

Extra keys are ignored throughout: the API returns far more of each object
than this project reads, and a field arriving that is not declared here is
not an error — it is simply not something inkwell looks at. Every field has a
default for the same reason the hand-rolled version needed guards: the API
omits what does not apply rather than sending an empty value.
"""

from pydantic import BaseModel, ConfigDict, Field


class DocsModel(BaseModel):
    """One Docs API object, read by the names the API sends."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)


class Link(DocsModel):
    """Where a styled run points."""

    url: str = ""


class TextStyle(DocsModel):
    """The character formatting inkwell round-trips through markdown."""

    bold: bool = False
    italic: bool = False
    link: Link = Link()


class TextRun(DocsModel):
    """One run of text sharing a single style."""

    content: str = ""
    text_style: TextStyle = Field(default=TextStyle(), alias="textStyle")


class ParagraphElement(DocsModel):
    """One piece of a paragraph — a text run, or something inkwell skips."""

    text_run: TextRun | None = Field(default=None, alias="textRun")


HEADING_STYLES = (
    "HEADING_1",
    "HEADING_2",
    "HEADING_3",
    "HEADING_4",
    "HEADING_5",
    "HEADING_6",
)
"""What Docs calls each heading depth, in the order the depths run."""


class ParagraphStyle(DocsModel):
    """Paragraph-level formatting, of which only the named style is read."""

    named_style_type: str = Field(default="", alias="namedStyleType")

    def heading_level(self) -> int | None:
        """Which heading depth this style names, if it names one.

        Derived from the order of the names rather than from a second table
        pairing each with a number, so the two cannot come to disagree.
        """
        if self.named_style_type not in HEADING_STYLES:
            return None
        return HEADING_STYLES.index(self.named_style_type) + 1


class Bullet(DocsModel):
    """A paragraph's membership in a list, at one nesting depth."""

    list_id: str = Field(default="", alias="listId")
    nesting_level: int = Field(default=0, alias="nestingLevel")


class Paragraph(DocsModel):
    """One paragraph: its runs, its style, and any list it belongs to."""

    elements: list[ParagraphElement] = Field(default_factory=list)
    paragraph_style: ParagraphStyle = Field(
        default=ParagraphStyle(), alias="paragraphStyle"
    )
    bullet: Bullet | None = None


class StructuralElement(DocsModel):
    """One element of a body — a paragraph, or something inkwell skips."""

    end_index: int = Field(default=0, alias="endIndex")
    paragraph: Paragraph | None = None


class Body(DocsModel):
    """A tab's ordered content."""

    content: list[StructuralElement] = Field(default_factory=list)


class NestingLevel(DocsModel):
    """How one depth of a list marks its items."""

    glyph_type: str = Field(default="", alias="glyphType")

    def ordered(self) -> bool:
        """Whether this depth numbers its items rather than bulleting them."""
        return self.glyph_type not in ("", "GLYPH_TYPE_UNSPECIFIED")


class ListProperties(DocsModel):
    """One list's marking, per nesting depth."""

    nesting_levels: list[NestingLevel] = Field(
        default_factory=list, alias="nestingLevels"
    )


class ListDefinition(DocsModel):
    """One list a tab's paragraphs can belong to."""

    list_properties: ListProperties = Field(
        default=ListProperties(), alias="listProperties"
    )


class DocumentTab(DocsModel):
    """The document one tab holds."""

    body: Body = Body()
    lists: dict[str, ListDefinition] = Field(default_factory=dict)


class TabProperties(DocsModel):
    """How a tab identifies itself."""

    tab_id: str = Field(default="", alias="tabId")
    title: str = ""


class Tab(DocsModel):
    """One tab, its content, and the tabs nested under it."""

    tab_properties: TabProperties = Field(
        default=TabProperties(), alias="tabProperties"
    )
    document_tab: DocumentTab = Field(default=DocumentTab(), alias="documentTab")
    child_tabs: list["Tab"] = Field(default_factory=list, alias="childTabs")

    def ordered_list(self, bullet: Bullet) -> bool:
        """Whether the list this bullet belongs to numbers its items."""
        definition = self.document_tab.lists.get(
            bullet.list_id
        )  # lup: ignore[dict-get] — lists are keyed by ids the document invents
        if definition is None:
            return False
        levels = definition.list_properties.nesting_levels
        if bullet.nesting_level >= len(levels):
            return False
        return levels[bullet.nesting_level].ordered()

    def paragraphs(self) -> list[Paragraph]:
        """Every paragraph in this tab, in document order."""
        return [
            element.paragraph
            for element in self.document_tab.body.content
            if element.paragraph is not None
        ]

    def end_index(self) -> int:
        """Where this tab's content ends, which is what clearing it needs."""
        return max(
            (element.end_index for element in self.document_tab.body.content),
            default=1,
        )

    def find(self, tab_id: str) -> "Tab | None":
        """This tab or the first descendant carrying that id."""
        if self.tab_properties.tab_id == tab_id:
            return self
        for child in self.child_tabs:
            found = child.find(tab_id)
            if found is not None:
                return found
        return None


class Document(DocsModel):
    """A whole document, as far as inkwell reads it."""

    document_id: str = Field(default="", alias="documentId")
    title: str = ""
    tabs: list[Tab] = Field(default_factory=list)

    def walk(self) -> "list[Tab]":
        """Every tab in the document, parents before the tabs nested in them."""

        def descend(tabs: list[Tab]) -> list[Tab]:
            return [found for tab in tabs for found in [tab, *descend(tab.child_tabs)]]

        return descend(self.tabs)


class AddDocumentTabReply(DocsModel):
    """What creating a tab reports back about the tab it created."""

    tab_properties: TabProperties = Field(
        default=TabProperties(), alias="tabProperties"
    )


class BatchUpdateReply(DocsModel):
    """One reply in a batch, of which inkwell reads only tab creation.

    Both spellings appear because the API answers ``addDocumentTab`` and
    ``addTab`` requests with replies named after the request that made them.
    """

    add_document_tab: AddDocumentTabReply | None = Field(
        default=None, alias="addDocumentTab"
    )
    add_tab: AddDocumentTabReply | None = Field(default=None, alias="addTab")

    def created_tab_id(self) -> str:
        """The id of the tab this reply created, whichever way it was asked."""
        for created in (self.add_document_tab, self.add_tab):
            if created is not None:
                return created.tab_properties.tab_id
        return ""


class BatchUpdateResponse(DocsModel):
    """What a batch of document updates reports back."""

    replies: list[BatchUpdateReply] = Field(default_factory=list)

    def created_tab_id(self) -> str:
        """The id of the tab this batch created, if it created one."""
        for reply in self.replies:
            if created := reply.created_tab_id():
                return created
        return ""


class CommentAuthor(DocsModel):
    """Who wrote a comment, as Drive names them."""

    display_name: str = Field(default="", alias="displayName")


class QuotedFileContent(DocsModel):
    """The document text a comment is anchored to."""

    value: str = ""


class CommentReply(DocsModel):
    """One reply under a comment."""

    id: str = ""
    content: str = ""


class Comment(DocsModel):
    """One Drive comment on the document."""

    id: str = ""
    content: str = ""
    resolved: bool = False
    author: CommentAuthor = CommentAuthor()
    quoted_file_content: QuotedFileContent | None = Field(
        default=None, alias="quotedFileContent"
    )
    replies: list[CommentReply] = Field(default_factory=list)

    def anchor_text(self) -> str:
        """The quoted text this comment hangs off, or nothing if unanchored."""
        return (
            "" if self.quoted_file_content is None else self.quoted_file_content.value
        )


class CommentPage(DocsModel):
    """One page of the document's comments."""

    comments: list[Comment] = Field(default_factory=list)
    next_page_token: str = Field(default="", alias="nextPageToken")


class CreatedComment(DocsModel):
    """What creating a comment or reply reports back."""

    id: str = ""


class UploadedFile(DocsModel):
    """What uploading a file to Drive reports back."""

    id: str = ""
