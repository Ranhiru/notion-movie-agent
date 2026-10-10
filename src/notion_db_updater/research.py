"""Public web research for the conversational Watchlist tools.

Search uses Tavily's general `/search` endpoint. Page retrieval pins each connection to a
validated public DNS address, follows redirects manually, and extracts bounded HTML text.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from aiolimiter import AsyncLimiter

from .config import Settings
from .search import post_json

_TAVILY_API = "https://api.tavily.com"
_MAX_RESULTS = 5
_MAX_REDIRECTS = 5
_MAX_PAGE_BYTES = 1_000_000
_MAX_PAGE_TEXT = 1_000_000
_REDIRECTS = {301, 302, 303, 307, 308}
_DROP_CONTENT = {"head", "script", "style", "noscript", "svg", "template"}
_SPACE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    """Collect readable HTML text without script, style, or metadata content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self._skip_depth += 1
        elif not self._skip_depth and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._skip_depth and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _validated_url(url: str) -> tuple[str, str, int, str]:
    """Return a normalized URL target, rejecting ambiguous or unsafe URL forms."""
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("URL is empty or too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url) or "\\" in url:
        raise ValueError("URL contains forbidden characters")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is malformed") from exc
    if (
        scheme not in {"http", "https"}
        or not host
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Only public HTTP and HTTPS URLs are allowed")
    expected_port = 443 if scheme == "https" else 80
    if port not in (None, expected_port):
        raise ValueError("Non-standard URL ports are not allowed")
    if "%" in host:  # Reject IPv6 zone identifiers and encoded host ambiguity.
        raise ValueError("Scoped or encoded host names are not allowed")

    try:
        normalized_host = str(ipaddress.ip_address(host))
    except ValueError:
        try:
            normalized_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("URL host name is invalid") from exc
        if (
            not normalized_host
            or normalized_host == "localhost"
            or normalized_host.endswith(".localhost")
        ):
            raise ValueError("Local host names are not allowed") from None

    path = parsed.path or "/"
    target = urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))
    return target, scheme, expected_port, normalized_host


async def _public_addresses(host: str, port: int) -> list[str]:
    """Resolve once and reject the whole answer if any address is not globally routable."""
    try:
        literal = ipaddress.ip_address(host)
        candidates = [str(literal)]
    except ValueError:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        candidates = [record[4][0] for record in records]
    addresses: list[str] = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError("DNS returned an invalid address") from exc
        if not address.is_global:
            raise ValueError("URL resolves to a non-public network address")
        value = str(address)
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise ValueError("URL host has no public addresses")
    return addresses


def _pinned_request_url(url: str, address: str, port: int) -> str:
    parsed = urlsplit(url)
    rendered_ip = f"[{address}]" if ":" in address else address
    authority = f"{rendered_ip}:{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ""))


async def _response_bytes(response: httpx.Response, maximum: int) -> bytes:
    """Read at most `maximum` decoded response bytes."""
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise ValueError("Web page has an invalid content length") from exc
        if declared_size > maximum:
            raise ValueError("Web page exceeds the retrieval size limit")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise ValueError("Web page exceeds the retrieval size limit")
        chunks.append(chunk)
    return b"".join(chunks)


class ResearchClient:
    """Tavily search and guarded public-page retrieval for conversational research."""

    def __init__(
        self,
        settings: Settings,
        *,
        search_http: httpx.AsyncClient | None = None,
        page_http: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_search_http = search_http is None
        self._owns_page_http = page_http is None
        self._search_http = search_http or httpx.AsyncClient(
            base_url=_TAVILY_API,
            headers={
                "Authorization": f"Bearer {settings.TAVILY_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
            trust_env=False,
        )
        self._page_http = page_http or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._limiter = AsyncLimiter(settings.TAVILY_RPM, 60)
        self._max_retries = settings.RETRY_MAX_ATTEMPTS

    async def __aenter__(self) -> ResearchClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_search_http:
            await self._search_http.aclose()
        if self._owns_page_http:
            await self._page_http.aclose()

    async def search(self, query: str) -> list[dict[str, str]]:
        """Search the public web, returning at most five `{url, title, content}` results."""
        data = await post_json(
            self._search_http,
            "/search",
            {
                "query": query,
                "max_results": _MAX_RESULTS,
                "search_depth": "basic",
                "include_raw_content": False,
            },
            limiter=self._limiter,
            max_retries=self._max_retries,
        )
        results = data.get("results") or []
        return [
            {
                "url": str(result.get("url") or ""),
                "title": str(result.get("title") or ""),
                "content": str(result.get("content") or ""),
            }
            for result in results[:_MAX_RESULTS]
            if isinstance(result, dict)
        ]

    async def fetch_page(self, url: str) -> str:
        """Fetch and clean a public HTML page with validated, pinned redirects."""
        current_url = url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            normalized, scheme, port, host = _validated_url(current_url)
            addresses = await _public_addresses(host, port)
            request_url = _pinned_request_url(normalized, addresses[0], port)
            host_header = f"[{host}]" if ":" in host else host
            headers = {
                "Host": host_header,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "User-Agent": "NotionMovieAgent/1.0",
            }
            extensions = {"sni_hostname": host} if scheme == "https" else None
            async with self._page_http.stream(
                "GET",
                request_url,
                headers=headers,
                follow_redirects=False,
                extensions=extensions,
            ) as response:
                if response.status_code in _REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("Redirect has no destination")
                    if redirect_count == _MAX_REDIRECTS:
                        raise ValueError("Web page exceeded the redirect limit")
                    current_url = urljoin(normalized, location)
                    continue
                response.raise_for_status()
                media_type = (
                    response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                )
                if media_type not in {"text/html", "application/xhtml+xml"}:
                    raise ValueError("URL did not return an HTML page")
                body = await _response_bytes(response, _MAX_PAGE_BYTES)

            encoding = response.encoding or "utf-8"
            html = body.decode(encoding, errors="replace")
            parser = _TextExtractor()
            parser.feed(html)
            parser.close()
            text = _SPACE.sub(" ", " ".join(parser.parts)).strip()
            return text[:_MAX_PAGE_TEXT]
        raise AssertionError("unreachable: redirect loop returns or raises")
