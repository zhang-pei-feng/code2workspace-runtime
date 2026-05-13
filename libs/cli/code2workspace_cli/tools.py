"""Custom tools for the CLI agent."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from tavily import TavilyClient

_UNSET = object()
_tavily_client: TavilyClient | object | None = _UNSET


def _get_tavily_client() -> TavilyClient | None:
    """Get or initialize the lazy Tavily client singleton.

    Returns:
        TavilyClient instance, or None if API key is not configured.
    """
    global _tavily_client  # noqa: PLW0603  # Module-level cache requires global statement
    if _tavily_client is not _UNSET:
        return _tavily_client  # type: ignore[return-value]  # narrowed by sentinel check

    from code2workspace_cli.config import settings

    if settings.has_tavily:
        from tavily import TavilyClient as _TavilyClient

        _tavily_client = _TavilyClient(api_key=settings.tavily_api_key)
    else:
        _tavily_client = None
    return _tavily_client


def web_search(  # noqa: ANN201  # Return type depends on dynamic tool configuration
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = False,
):
    """Search the web using Tavily for current information and documentation.

    This tool searches the web and returns relevant results. After receiving results,
    you MUST synthesize the information into a natural, helpful response for the user.

    Args:
        query: The search query (be specific and detailed)
        max_results: Number of results to return (default: 5)
        topic: Search topic type - "general" for most queries, "news" for current events
        include_raw_content: Include full page content (warning: uses more tokens)

    Returns:
        Dictionary containing:
        - results: List of search results, each with:
            - title: Page title
            - url: Page URL
            - content: Relevant excerpt from the page
            - score: Relevance score (0-1)
        - query: The original search query

    IMPORTANT: After using this tool:
    1. Read through the 'content' field of each result
    2. Extract relevant information that answers the user's question
    3. Synthesize this into a clear, natural language response
    4. Cite sources by mentioning the page titles or URLs
    5. NEVER show the raw JSON to the user - always provide a formatted response
    """
    try:
        import requests
        from tavily import (
            BadRequestError,
            InvalidAPIKeyError,
            MissingAPIKeyError,
            UsageLimitExceededError,
        )
        from tavily.errors import ForbiddenError, TimeoutError as TavilyTimeoutError
    except ImportError as exc:
        return {
            "error": f"Required package not installed: {exc.name}. "
            "Install with: pip install 'code2workspace[cli]'",
            "query": query,
        }

    client = _get_tavily_client()
    if client is None:
        return {
            "error": "Tavily API key not configured. "
            "Please set TAVILY_API_KEY environment variable.",
            "query": query,
        }

    try:
        return client.search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic=topic,
        )
    except (
        requests.exceptions.RequestException,
        ValueError,
        TypeError,
        # Tavily-specific exceptions
        BadRequestError,
        ForbiddenError,
        InvalidAPIKeyError,
        MissingAPIKeyError,
        TavilyTimeoutError,
        UsageLimitExceededError,
    ) as e:
        return {"error": f"Web search error: {e!s}", "query": query}


def fetch_url(url: str, timeout: int = 30) -> dict[str, Any]:
    """Fetch content from a URL and convert HTML to markdown format.

    This tool fetches web page content and converts it to clean markdown text,
    making it easy to read and process HTML content. After receiving the markdown,
    you MUST synthesize the information into a natural, helpful response for the user.

    Args:
        url: The URL to fetch (must be a valid HTTP/HTTPS URL)
        timeout: Request timeout in seconds (default: 30)

    Returns:
        Dictionary containing:
        - success: Whether the request succeeded
        - url: The final URL after redirects
        - markdown_content: The page content converted to markdown
        - status_code: HTTP status code
        - content_length: Length of the markdown content in characters

    IMPORTANT: After using this tool:
    1. Read through the markdown content
    2. Extract relevant information that answers the user's question
    3. Synthesize this into a clear, natural language response
    4. NEVER show the raw markdown to the user unless specifically requested
    """
    try:
        import requests
        from bs4 import BeautifulSoup, UnicodeDammit
        from markdownify import markdownify
    except ImportError as exc:
        return {
            "error": f"Required package not installed: {exc.name}. "
            "Install with: pip install 'code2workspace[cli]'",
            "url": url,
        }

    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Code2Workspace/1.0)"},
        )
        response.raise_for_status()

        content_bytes = response.content
        content_type = response.headers.get("content-type", "")

        # Decode bytes conservatively so legacy/Chinese government pages do not
        # collapse into mojibake when servers omit or misreport charset.
        decoded_text: str
        if _looks_like_textual_payload(content_type, content_bytes):
            decoded_text = _decode_response_text(
                content_bytes=content_bytes,
                content_type=content_type,
                fallback_encoding=getattr(response, "encoding", None),
            )
        else:
            decoded_text = response.text

        if _looks_like_html(content_type, decoded_text):
            try:
                markdown_content = markdownify(decoded_text)
            except RecursionError:
                markdown_content = _html_to_text_fallback(decoded_text, BeautifulSoup)
        else:
            markdown_content = decoded_text

        return {
            "url": str(response.url),
            "markdown_content": markdown_content,
            "status_code": response.status_code,
            "content_length": len(markdown_content),
        }
    except requests.exceptions.RequestException as e:
        return {"error": f"Fetch URL error: {e!s}", "url": url}


def _looks_like_textual_payload(content_type: str, content_bytes: bytes) -> bool:
    lowered = content_type.casefold()
    if any(
        marker in lowered
        for marker in (
            "text/",
            "html",
            "xml",
            "json",
            "javascript",
            "pdf",
        )
    ):
        return True
    return b"<html" in content_bytes[:512].casefold() or b"<!doctype html" in content_bytes[:512].casefold()


def _looks_like_html(content_type: str, decoded_text: str) -> bool:
    lowered = content_type.casefold()
    if "html" in lowered or "xml" in lowered:
        return True
    prefix = decoded_text[:512].casefold()
    return "<html" in prefix or "<body" in prefix or "<!doctype html" in prefix


def _decode_response_text(
    *,
    content_bytes: bytes,
    content_type: str,
    fallback_encoding: str | None,
) -> str:
    from bs4 import UnicodeDammit

    header_encoding = _charset_from_content_type(content_type)
    encodings_to_try: list[str | None] = [header_encoding]
    if header_encoding is not None:
        encodings_to_try.append(fallback_encoding)
    elif fallback_encoding and fallback_encoding.casefold() not in {"iso-8859-1", "latin-1"}:
        encodings_to_try.append(fallback_encoding)

    for encoding in encodings_to_try:
        if not encoding:
            continue
        try:
            return content_bytes.decode(encoding, errors="replace")
        except LookupError:
            continue

    unicode_dammit = UnicodeDammit(content_bytes)
    if unicode_dammit.unicode_markup and not _looks_like_mojibake(unicode_dammit.unicode_markup):
        return unicode_dammit.unicode_markup

    best_text = ""
    best_score = -1
    for encoding in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            candidate = content_bytes.decode(encoding, errors="replace")
        except LookupError:
            continue
        score = _decoded_text_score(candidate)
        if score > best_score:
            best_text = candidate
            best_score = score

    try:
        from charset_normalizer import from_bytes

        best_match = from_bytes(content_bytes).best()
        if best_match is not None:
            candidate = str(best_match)
            score = _decoded_text_score(candidate)
            if score > best_score:
                best_text = candidate
                best_score = score
    except Exception:
        pass

    if best_text:
        return best_text
    return content_bytes.decode("utf-8", errors="replace")


def _charset_from_content_type(content_type: str) -> str | None:
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def _html_to_text_fallback(decoded_text: str, soup_cls) -> str:
    soup = soup_cls(decoded_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _looks_like_mojibake(text: str) -> bool:
    if not text:
        return False
    bad_markers = sum(text.count(marker) for marker in ("Ã", "Â", "Ô", "Õ", "Ð", "Ê", "Î", "Ï", "æ", "è", "ã"))
    cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return bad_markers >= 3 and cjk_chars == 0


def _decoded_text_score(text: str) -> int:
    cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    replacement_chars = text.count("\ufffd")
    mojibake_penalty = 20 if _looks_like_mojibake(text) else 0
    return (cjk_chars * 4) - (replacement_chars * 3) - mojibake_penalty + len(text[:200])
