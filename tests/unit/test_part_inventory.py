"""What a part carries, counted so a rewrite cannot quietly stop carrying it."""

from inkwell.manuscript.inventory import inventory_of

FIGURE = """\
## 2.3.2 Cyber Risk

Some standing prose about cyber risk, citing [Zhu et al.](https://arxiv.org/abs/2105.04932).

<figure markdown="span">
![Enter image alt description](Images/kaS_Image_12.png){ loading=lazy }
  <figcaption markdown="1"><b>Figure 2.12:</b> Example of one shot face swapping. \
([Li et al., 2024](https://arxiv.org/abs/2403.03218))</figcaption>
</figure>

More prose.
"""


class TestAFigureIsFoundThroughBothParsers:
    """A figure here is an HTML block holding markdown, which is one format
    nested in the other — a reader that stopped at either finds nothing."""

    def test_the_image_inside_an_html_figure_is_found(self) -> None:
        held = inventory_of(FIGURE)

        assert held.images() == ("Images/kaS_Image_12.png",)

    def test_the_number_comes_off_the_caption_rather_than_its_prose(self) -> None:
        """Read off the element, so a caption mentioning another figure by name
        does not rename this one."""
        held = inventory_of(FIGURE)

        assert held.figures[0].number == "Figure 2.12"

    def test_the_whole_block_travels_verbatim(self) -> None:
        """A writer handed only the number and the path composes a figure of its
        own, and the book acquires two layouts."""
        held = inventory_of(FIGURE)

        assert "figcaption" in held.figures[0].block
        assert "loading=lazy" in held.figures[0].block

    def test_a_bare_image_counts_as_a_figure_too(self) -> None:
        """Dropping it loses exactly as much, and it is the one nobody notices."""
        held = inventory_of("## A part\n\n![](Images/plain.png)\n")

        assert held.images() == ("Images/plain.png",)

    def test_a_figure_is_counted_once_however_it_parses(self) -> None:
        held = inventory_of(FIGURE)

        assert len(held.figures) == 1


class TestACitationInACaptionIsStillACitation:
    """It is the one a rewrite loses by reproducing a figure from a description
    of it rather than from the figure."""

    def test_prose_citations_are_found(self) -> None:
        assert "https://arxiv.org/abs/2105.04932" in inventory_of(FIGURE).citations

    def test_caption_citations_are_found(self) -> None:
        assert "https://arxiv.org/abs/2403.03218" in inventory_of(FIGURE).citations

    def test_each_url_is_carried_once(self) -> None:
        twice = "[a](https://one.test/x) and again [a](https://one.test/x)"

        assert inventory_of(twice).citations == ("https://one.test/x",)


class TestWhatOneDraftLostAgainstAnother:
    """ "Nothing was lost" is a subtraction, not an assurance."""

    def test_a_dropped_figure_is_named(self) -> None:
        lost = inventory_of(FIGURE).lost_to(inventory_of("## 2.3.2 Cyber Risk\n"))

        assert [one.number for one in lost.figures] == ["Figure 2.12"]

    def test_a_dropped_citation_is_named(self) -> None:
        lost = inventory_of(FIGURE).lost_to(inventory_of("## 2.3.2 Cyber Risk\n"))

        assert "https://arxiv.org/abs/2403.03218" in lost.citations

    def test_a_draft_that_kept_everything_lost_nothing(self) -> None:
        assert inventory_of(FIGURE).lost_to(inventory_of(FIGURE)).empty()

    def test_a_draft_that_grew_is_not_short_of_words(self) -> None:
        """The shortfall is what is missing, not the later draft's own length."""
        longer = FIGURE + "\n\nA good deal more prose than there was before.\n"

        assert inventory_of(FIGURE).lost_to(inventory_of(longer)).words == 0
