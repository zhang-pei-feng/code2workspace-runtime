#!/usr/bin/env python3
"""Small CLI for the EpiETL Epidemic Intelligence Data API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = "https://epietl.com"
PATHS = {
    "health": "/api/health",
    "channels": "/api/channels",
    "events": "/api/risk/events",
    "reports": "/api/reports",
}


def add_if_present(params: dict[str, str], key: str, value: object | None) -> None:
    if value is not None:
        params[key] = str(value)


def parse_extra_params(values: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"--param must be key=value, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--param key cannot be empty, got: {item}")
        params[key] = value
    return params


def build_url(endpoint: str, params: dict[str, str]) -> str:
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}{PATHS[endpoint]}"
    return f"{url}?{query}" if query else url


def request_json(url: str, api_key: str | None, timeout: float) -> tuple[int, object]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "openclaw-epietl-api-skill/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    try:
        return status, json.loads(body) if body else None
    except json.JSONDecodeError:
        return status, {"raw": body}


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the EpiETL API.")
    parser.add_argument("endpoint", choices=sorted(PATHS))
    parser.add_argument("--country")
    parser.add_argument("--pathogen")
    parser.add_argument("--severity")
    parser.add_argument("--channel")
    parser.add_argument("--query", "--q", dest="query")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int)
    parser.add_argument("--param", action="append", default=[], help="Extra query parameter as key=value.")
    parser.add_argument("--api-key", default=os.environ.get("EPIETL_API_KEY"), help="Defaults to EPIETL_API_KEY.")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--url-only", action="store_true", help="Print the request URL and exit.")
    args = parser.parse_args()

    if args.limit is not None and not 1 <= args.limit <= 200:
        raise SystemExit("--limit must be between 1 and 200")

    params = parse_extra_params(args.param)
    add_if_present(params, "country", args.country)
    add_if_present(params, "pathogen", args.pathogen)
    add_if_present(params, "severity", args.severity)
    add_if_present(params, "channel", args.channel)
    add_if_present(params, "q", args.query)
    add_if_present(params, "limit", args.limit)
    add_if_present(params, "offset", args.offset)

    if args.endpoint == "reports" and not args.api_key:
        raise SystemExit("reports requires an API key. Set EPIETL_API_KEY or pass --api-key.")

    url = build_url(args.endpoint, params)
    if args.url_only:
        print(url)
        return 0

    status, payload = request_json(url, args.api_key, args.timeout)
    print(json.dumps({"status": status, "url": url, "data": payload}, ensure_ascii=False, indent=2))
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
