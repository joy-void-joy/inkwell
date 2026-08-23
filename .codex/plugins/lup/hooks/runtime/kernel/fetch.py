"""URL scope matching for the fetch policy."""

import urllib.parse

from .decision import KernelDecision
from .rows import UrlScopeRow


def host_matches_scope(hostname: str, expected_host: str, subdomains: bool) -> bool:
    """Match a host exactly, or beneath a scope that opted into subdomains.

    The leading dot is required so a scope for ``githubusercontent.com``
    covers ``raw.githubusercontent.com`` without also covering a
    lookalike registration like ``evilgithubusercontent.com``.
    """
    return hostname == expected_host or (
        subdomains and hostname.endswith(f".{expected_host}")
    )


def url_matches_scope(
    scheme: str,
    hostname: str,
    port: int | None,
    path: str,
    scope: UrlScopeRow,
) -> bool:
    """Compare parsed URL components with one primitive scope row."""
    return (
        scheme == scope["scheme"]
        and host_matches_scope(hostname, scope["host"], scope["include_subdomains"])
        and (scope["any_port"] or port == scope["port"])
        and path.startswith(scope["path_prefix"])
    )


def decide_fetch(
    url: str,
    allowed_scopes: list[UrlScopeRow],
    denied_scopes: list[UrlScopeRow],
) -> KernelDecision:
    """Deny matching scopes first, allow declared scopes, and ask otherwise."""
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return KernelDecision("ask", "malformed URL requires approval")
    if not parsed.scheme or hostname is None:
        return KernelDecision("ask", "malformed URL requires approval")
    denied = next(
        (
            scope
            for scope in denied_scopes
            if url_matches_scope(parsed.scheme, hostname, port, parsed.path, scope)
        ),
        None,
    )
    if denied is not None:
        return KernelDecision("deny", denied["reason"] or "URL is denied")
    allowed = next(
        (
            scope
            for scope in allowed_scopes
            if url_matches_scope(parsed.scheme, hostname, port, parsed.path, scope)
        ),
        None,
    )
    if allowed is not None:
        return KernelDecision("allow", allowed["reason"])
    return KernelDecision("ask", "URL is outside the declared documentation scopes")
