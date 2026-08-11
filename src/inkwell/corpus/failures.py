"""Name what went wrong, so a bulk run stays diagnosable.

A sync of thousands of documents produces failures, and "12 failed" tells an
operator nothing about whether to wait, fix a declaration, or fix the code.
These classes are the vocabulary that distinguishes those: rate limiting means
wait, a 404 means the enumeration is out of date, and a code error means this
module's own bug.

Recorded per document in the source's index, so the next run retries and the
class travels with the retry rather than being re-derived from a stale message.
"""

import httpx

from inkwell.corpus.fetch import FetchRefused

TIMEOUT = "timeout"
NETWORK = "network"
RATE_LIMITED = "rate_limited"
UPSTREAM_MISSING = "upstream_missing"
UPSTREAM_CLIENT = "upstream_client"
UPSTREAM_SERVER = "upstream_server"
UPSTREAM_OTHER = "upstream_other"
REFUSED = "refused"
STORE_ERROR = "store_error"
CODE_ERROR = "code_error"

FAILURE_CLASSES: tuple[str, ...] = (
    TIMEOUT,
    NETWORK,
    RATE_LIMITED,
    UPSTREAM_MISSING,
    UPSTREAM_CLIENT,
    UPSTREAM_SERVER,
    UPSTREAM_OTHER,
    REFUSED,
    STORE_ERROR,
    CODE_ERROR,
)
"""Every class a failure can be given. Stable, because an operator learns them."""

TOO_MANY_REQUESTS = 429
NOT_FOUND = 404
CLIENT_ERROR_FLOOR = 400
SERVER_ERROR_FLOOR = 500
SERVER_ERROR_CEILING = 600


def status_class(status: int) -> str:
    """The class an HTTP status belongs to."""
    if status == TOO_MANY_REQUESTS:
        return RATE_LIMITED
    if status == NOT_FOUND:
        return UPSTREAM_MISSING
    if SERVER_ERROR_FLOOR <= status < SERVER_ERROR_CEILING:
        return UPSTREAM_SERVER
    if CLIENT_ERROR_FLOOR <= status < SERVER_ERROR_FLOOR:
        return UPSTREAM_CLIENT
    return UPSTREAM_OTHER


def classify(error: BaseException) -> str:
    """Which failure class an exception from a fetch or a store belongs to."""
    match error:
        case httpx.TimeoutException():
            return TIMEOUT
        case httpx.HTTPStatusError():
            return status_class(error.response.status_code)
        case httpx.HTTPError():
            return NETWORK
        case FetchRefused():
            return REFUSED
        case OSError():
            return STORE_ERROR
        case ValueError():
            return STORE_ERROR
        case _:
            return CODE_ERROR
