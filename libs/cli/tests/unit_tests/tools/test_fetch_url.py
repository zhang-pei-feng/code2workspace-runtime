"""Tests for tools module."""

import requests
import responses
from unittest.mock import patch

from code2workspace_cli.tools import fetch_url


@responses.activate
def test_fetch_url_success() -> None:
    """Test successful URL fetch and HTML to markdown conversion."""
    responses.add(
        responses.GET,
        "http://example.com",
        body="<html><body><h1>Test</h1><p>Content</p></body></html>",
        status=200,
    )

    result = fetch_url("http://example.com")

    assert result["status_code"] == 200
    assert "Test" in result["markdown_content"]
    assert result["url"].startswith("http://example.com")
    assert result["content_length"] > 0


@responses.activate
def test_fetch_url_http_error() -> None:
    """Test handling of HTTP errors."""
    responses.add(
        responses.GET,
        "http://example.com/notfound",
        status=404,
    )

    result = fetch_url("http://example.com/notfound")

    assert "error" in result
    assert "Fetch URL error" in result["error"]
    assert result["url"] == "http://example.com/notfound"


@responses.activate
def test_fetch_url_timeout() -> None:
    """Test handling of request timeout."""
    responses.add(
        responses.GET,
        "http://example.com/slow",
        body=requests.exceptions.Timeout(),
    )

    result = fetch_url("http://example.com/slow", timeout=1)

    assert "error" in result
    assert "Fetch URL error" in result["error"]
    assert result["url"] == "http://example.com/slow"


@responses.activate
def test_fetch_url_connection_error() -> None:
    """Test handling of connection errors."""
    responses.add(
        responses.GET,
        "http://example.com/error",
        body=requests.exceptions.ConnectionError(),
    )

    result = fetch_url("http://example.com/error")

    assert "error" in result
    assert "Fetch URL error" in result["error"]
    assert result["url"] == "http://example.com/error"


@responses.activate
def test_fetch_url_decodes_gb18030_html_without_charset_header() -> None:
    body = "<html><body><h1>交通运输部</h1><p>春运人员流动量预计达95亿人次</p></body></html>".encode(
        "gb18030"
    )
    responses.add(
        responses.GET,
        "http://example.com/gbk",
        body=body,
        status=200,
        content_type="text/html",
    )

    result = fetch_url("http://example.com/gbk")

    assert "交通运输部" in result["markdown_content"]
    assert "95亿人次" in result["markdown_content"]


@responses.activate
def test_fetch_url_falls_back_to_plain_text_on_markdownify_recursion() -> None:
    responses.add(
        responses.GET,
        "http://example.com/recursive",
        body="<html><body><h1>Title</h1><p>Body</p></body></html>",
        status=200,
    )

    with patch("markdownify.markdownify", side_effect=RecursionError("boom")):
        result = fetch_url("http://example.com/recursive")

    assert "Title" in result["markdown_content"]
    assert "Body" in result["markdown_content"]
