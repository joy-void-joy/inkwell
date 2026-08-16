"""What can be read off a PDF without reading text out of it.

A page count is structure, not content: poppler reports it from the page
tree, so a scanned document answers the same as a born-digital one. That is
the whole of what belongs here — pulling the *text* out is what returns an
empty string from an image-only page, and an empty string reads as a document
that does not say the thing rather than as an extraction that failed. Both the
corpus and the source registry need the count and neither needs the text, so
the one operation lives here instead of once in each.
"""

import logging
from configparser import ConfigParser
from pathlib import Path

import sh

logger = logging.getLogger(__name__)


def page_count(pdf_path: Path) -> int:
    """How many pages a PDF has, read from its metadata by ``pdfinfo``.

    ``pdfinfo`` emits ``Key: value`` lines, which is what :mod:`configparser`
    reads once given a section to hang them under. Its labels carry spaces
    (``Custom Metadata``, ``Page size``) and its values carry colons and
    percent signs, so interpolation is off and duplicate keys are tolerated
    rather than fatal — poppler is describing a document, not writing us a
    configuration file.

    Zero where poppler is absent or the file is unreadable, which reads
    downstream as "page windows unknown, take it whole". ``pdfinfo`` exits 0
    even on a file that is not a PDF, printing its complaint to stderr, so a
    missing count is what has to be caught rather than the exit status.
    """
    try:
        report = ConfigParser(interpolation=None, strict=False)
        report.read_string(f"[pdfinfo]\n{sh.Command('pdfinfo')(str(pdf_path))}")
    except (sh.ErrorReturnCode, sh.CommandNotFound, OSError):
        logger.warning("pdfinfo could not read %s", pdf_path, exc_info=True)
        return 0
    if not report.has_option("pdfinfo", "Pages"):
        logger.warning("pdfinfo reported no page count for %s", pdf_path)
        return 0
    return report.getint("pdfinfo", "Pages")
