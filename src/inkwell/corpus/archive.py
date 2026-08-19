"""Reading a document from a public web archive when its own host refuses us.

Some hosts publish openly and refuse automated clients anyway. `openai.com`
serves `robots.txt` saying `Allow: /` and advertises its sitemap, then answers an
identified crawler with a 403 or an anti-bot interstitial on every article — so
the site's stated policy invites the read and its edge declines it. That is a
gate on the connection, not a wish about the content, and a public archive that
already holds the page answers the same question without touching the host again.

**Not a way past a gate.** Nothing here defeats bot detection: no spoofed user
agent, no solved challenge, no attempt to look like a browser. It asks a
different party — one that already crawled and published the page — for the copy
it holds. A host that genuinely does not want to be read says so in `robots.txt`,
and a source whose `robots.txt` disallows a path has no business being declared
here in the first place.

**Provenance is recorded, not hidden.** An archived capture is weaker evidence
than a live fetch: it is a copy, taken at a stated time, that could be stale. So a
document fetched this way carries the snapshot it came from, and every surface
that shows the document can say so. A corpus whose archived documents were
indistinguishable from live ones would be one that quietly dated itself.

**One extra request per gated document.** The availability endpoint answers with
the closest capture in a single small call, which is why it is preferred over
walking the CDX index per URL. The archive is a free public service; a sweep
reaches it only for documents the host has already refused.
"""

import logging
from typing import Literal
from urllib.parse import urlunparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from lup.types import JsonObject, JsonValue

logger = logging.getLogger(__name__)

AVAILABILITY_ENDPOINT = "https://archive.org/wayback/available"
"""The Internet Archive's documented "closest capture" lookup.

One request per URL, answering with the nearest snapshot rather than the whole
capture history — which is what the CDX index is for and what this deliberately
avoids asking for.
"""

CAPTURE_HOST = "web.archive.org"
"""Where a capture is read from, which is not where availability is asked."""

RAW_CAPTURE_MARKER = "id_"
"""What asks a capture for the bytes as crawled, without the archive's own frame.

A snapshot URL without it returns the page wrapped in the archive's banner and
with its links rewritten, which would reach the extractor as part of the
document. With it, the response is what the host served when it was crawled.
"""

type ArchiveOutcome = Literal["found", "absent", "unavailable"]
"""Whether an archive lookup produced a capture, and why not where it did not.

``absent`` is the archive answering that it holds nothing for this URL;
``unavailable`` is the archive itself not answering. The distinction decides
whether retrying could go differently, which is exactly what a failure report
needs to say and what a bare ``None`` could not.
"""


class ArchivedCapture(BaseModel):
    """One snapshot a public archive holds of one URL."""

    model_config = ConfigDict(frozen=True)

    outcome: ArchiveOutcome = Field(description="Whether a capture was found")
    url: str = Field(default="", description="The snapshot to read the bytes from")
    timestamp: str = Field(
        default="", description="When the archive crawled it, as the archive states it"
    )
    detail: str = Field(
        default="", description="Why there is no capture, where there is none"
    )

    def found(self) -> bool:
        """Whether there is a snapshot to read."""
        return self.outcome == "found"


def raw_capture(url: str, timestamp: str) -> str:
    """The capture of ``url`` at ``timestamp``, asking for the bytes as crawled.

    Built from the original URL and the stamp rather than by editing the framed
    snapshot URL the archive replied with: the request is
    ``/web/<timestamp>id_/<url>``, so assembling it from its parts is both the
    documented form and immune to whichever host or scheme the reply named.
    """
    path = f"/web/{timestamp}{RAW_CAPTURE_MARKER}/{url}"
    return urlunparse(("https", CAPTURE_HOST, path, "", "", ""))


def stated(mapping: JsonObject, key: str) -> JsonValue:
    """One key of another service's payload, whose keys are its own to choose."""
    # lup: ignore[dict-get] — the archive's own reply, not a model of ours
    return mapping.get(key)


def read_capture(url: str, payload: JsonObject) -> ArchivedCapture:
    """What the availability endpoint said: a capture, or why there is none.

    The endpoint reports the status the *host* returned when it was crawled, and
    that is not always ``200`` even for a snapshot holding the whole article. So
    it is not read as a gate: whether a capture is usable is decided by reading
    it, exactly as a live response is, rather than by trusting a field about it.
    """
    snapshots = stated(payload, "archived_snapshots")
    if not isinstance(snapshots, dict):
        return ArchivedCapture(outcome="absent", detail="the archive listed no capture")
    closest = stated(snapshots, "closest")
    if not isinstance(closest, dict):
        return ArchivedCapture(outcome="absent", detail="the archive holds no capture")
    timestamp = stated(closest, "timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return ArchivedCapture(
            outcome="absent", detail="the archive named a capture with no timestamp"
        )
    return ArchivedCapture(
        outcome="found", url=raw_capture(url, timestamp), timestamp=timestamp
    )


async def closest_capture(client: httpx.AsyncClient, url: str) -> ArchivedCapture:
    """The newest capture a public archive holds of ``url``.

    Every way of not getting one is an answer rather than an exception: the
    caller is already handling a document its host refused, and an archive that
    is down is one more thing that did not work rather than a new kind of failure
    to classify.
    """
    try:
        response = await client.request(
            "GET", AVAILABILITY_ENDPOINT, params={"url": url}, follow_redirects=True
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as failure:
        logger.warning("Archive lookup for %s did not answer: %s", url, failure)
        return ArchivedCapture(outcome="unavailable", detail=str(failure))
    if not isinstance(payload, dict):
        return ArchivedCapture(
            outcome="unavailable", detail="the archive answered with no object"
        )
    return read_capture(url, payload)
