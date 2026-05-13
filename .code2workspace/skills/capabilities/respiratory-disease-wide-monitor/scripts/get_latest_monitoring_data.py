#!/usr/bin/env python3
"""Compatibility entry point for older prompts.

The canonical wide-monitor script is fetch_sources.py. This wrapper keeps older
sessions from failing when they call get_latest_monitoring_data.py.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
FETCH_SOURCES = SCRIPT_DIR / "fetch_sources.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compatibility wrapper for fetch_sources.py")
    parser.add_argument("--region", help="Region keyword to search, e.g. 中国, Europe, Australia")
    parser.add_argument("--query", help="Source-table query keyword or phrase")
    parser.add_argument("--pathogen", help="Pathogen filter, e.g. 新冠, 流感, RSV")
    parser.add_argument("--category", help="Data source category filter")
    parser.add_argument("--limit", type=int, default=10, help="Limit number of sources")
    parser.add_argument("--child-limit", type=int, default=1, help="Fetch likely child pages/PDFs per source")
    parser.add_argument("--timeout", type=int, default=12, help="Per-request timeout seconds")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent fetch workers")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--output", type=Path, help="Optional output file path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    query_parts = [part for part in [args.query, args.region, args.pathogen] if part]

    command = [
        sys.executable,
        str(FETCH_SOURCES),
        "--format",
        args.format,
        "--limit",
        str(args.limit),
        "--child-limit",
        str(args.child_limit),
        "--timeout",
        str(args.timeout),
        "--workers",
        str(args.workers),
    ]
    if query_parts:
        command.extend(["--query", " ".join(query_parts)])
    if args.category:
        command.extend(["--category", args.category])
    if args.pathogen:
        command.extend(["--pathogen", args.pathogen])
    if args.output:
        command.extend(["--output", str(args.output)])

    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
