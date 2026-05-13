#!/usr/bin/env python3
"""Minimal local governance helper for project skillization."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SHARED_HELPER_DIR = Path(__file__).resolve().parents[3] / "_shared-superagent-helpers" / "scripts"
if str(SHARED_HELPER_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_HELPER_DIR))

from common import create_skill_run_dir, ensure_dir, latest_child_dir, read_json, write_json, write_text


SOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    "gisaid_epicov": {
        "purpose": "Restricted SARS-CoV-2 metadata source.",
        "restrictions": "Compliance review is mandatory; live fetch is not implemented locally.",
        "fetch_strategy": "metadata brief only",
        "supports_live_refresh": False,
        "sample_records": [{"accession": "EPI_ISL_000001", "country": "Unknown", "lineage": "demo"}],
    },
    "epietl": {
        "purpose": "Epidemiology intelligence feed with reports and events.",
        "restrictions": "Local helper only exposes metadata brief and optional local DB query.",
        "fetch_strategy": "metadata brief only",
        "supports_live_refresh": False,
        "sample_records": [{"report_id": "demo-report-1", "title": "Demo EpiETL report"}],
    },
    "ncbi_virus": {
        "purpose": "Public virus metadata source suitable for live refresh demos.",
        "restrictions": "Field completeness is limited and title parsing is best-effort.",
        "fetch_strategy": "NCBI eutils esearch + esummary",
        "supports_live_refresh": True,
        "sample_records": [{"accession": "PZ184894", "country": "Unknown", "lineage": ""}],
    },
    "sra": {
        "purpose": "Raw sequencing metadata source.",
        "restrictions": "Local helper only keeps light metadata and may miss host/platform details.",
        "fetch_strategy": "NCBI SRA esearch + esummary",
        "supports_live_refresh": False,
        "sample_records": [{"accession": "SRR000001", "country": "Unknown", "lineage": ""}],
    },
    "pango_lineages": {
        "purpose": "Lineage knowledge source rather than specimen inventory.",
        "restrictions": "This local helper provides metadata-only brief behavior.",
        "fetch_strategy": "metadata brief only",
        "supports_live_refresh": False,
        "sample_records": [{"lineage": "BA.2.86", "who_name": "Omicron"}],
    },
}


def _storage_root() -> Path:
    return ensure_dir(Path(__file__).resolve().parents[5] / "results" / "skills" / "data-governance-ops" / "snapshots")


def _source_root(source: str) -> Path:
    return ensure_dir(_storage_root() / source)


def _latest_snapshot_path(source: str) -> Path | None:
    latest = latest_child_dir(_source_root(source))
    if latest is None:
        return None
    candidate = latest / "snapshot.json"
    return candidate if candidate.exists() else None


def _previous_snapshot_path(source: str) -> Path | None:
    dirs = sorted(item for item in _source_root(source).iterdir() if item.is_dir())
    if len(dirs) < 2:
        return None
    candidate = dirs[-2] / "snapshot.json"
    return candidate if candidate.exists() else None


def _extract_country_from_title(title: str) -> str:
    if "/" not in title:
        return "Unknown"
    parts = title.split("/")
    return parts[1] if len(parts) > 1 and parts[1] else "Unknown"


def _fetch_ncbi_records(limit: int, query: str | None) -> list[dict[str, Any]]:
    term = query or "SARS-CoV-2[Organism]"
    esearch_params = urllib.parse.urlencode({
        "db": "nucleotide",
        "term": term,
        "retmax": str(limit),
        "retmode": "json",
    })
    esearch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{esearch_params}"
    with urllib.request.urlopen(esearch_url, timeout=30) as response:  # noqa: S310
        search_data = json.loads(response.read().decode("utf-8"))
    ids = search_data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    esummary_params = urllib.parse.urlencode({
        "db": "nucleotide",
        "id": ",".join(ids),
        "retmode": "json",
    })
    esummary_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?{esummary_params}"
    with urllib.request.urlopen(esummary_url, timeout=30) as response:  # noqa: S310
        summary_data = json.loads(response.read().decode("utf-8"))
    records = []
    for uid in ids:
        item = summary_data.get("result", {}).get(uid, {})
        title = str(item.get("title") or "")
        accession = str(item.get("caption") or uid)
        records.append(
            {
                "uid": uid,
                "accession": accession,
                "title": title,
                "organism": str(item.get("organism") or item.get("taxname") or ""),
                "country": _extract_country_from_title(title),
                "collection_date": "",
                "lineage": "",
                "raw": item,
            }
        )
    return records


def _refresh_snapshot(source: str, limit: int, query: str | None) -> dict[str, Any]:
    source_info = SOURCE_REGISTRY[source]
    if source == "ncbi_virus":
        records = _fetch_ncbi_records(limit=limit, query=query)
        mode = "live"
    else:
        records = source_info["sample_records"][:limit]
        mode = "sample"

    run_dir = create_skill_run_dir("data-governance-ops", "snapshots", source)
    payload = {
        "source": source,
        "mode": mode,
        "query": query,
        "record_count": len(records),
        "records": records,
    }
    write_json(run_dir / "snapshot.json", payload)
    write_text(run_dir / "snapshot.md", "# Snapshot\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n")
    return {"run_dir": str(run_dir), **payload}


def _query_records(records: list[dict[str, Any]], field: str, value: str, mode: str) -> list[dict[str, Any]]:
    matched = []
    for record in records:
        candidate = str(record.get(field, ""))
        if mode == "exact" and candidate == value:
            matched.append(record)
        elif mode == "contains" and value.lower() in candidate.lower():
            matched.append(record)
    return matched


def _quality_issues(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = []
    for record in records:
        accession = record.get("accession") or record.get("uid") or "unknown"
        if not str(record.get("country", "")).strip() or str(record.get("country")) == "Unknown":
            issues.append({"accession": accession, "code": "missing_country", "level": "warning"})
        if not str(record.get("collection_date", "")).strip():
            issues.append({"accession": accession, "code": "missing_collection_date", "level": "warning"})
    return issues


def cmd_source_brief(args: argparse.Namespace) -> int:
    payload = {"source": args.source, **SOURCE_REGISTRY[args.source]}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    payload = _refresh_snapshot(args.source, limit=args.limit, query=args.query)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_query_latest(args: argparse.Namespace) -> int:
    latest = _latest_snapshot_path(args.source)
    if latest is None:
        print(json.dumps({"ok": False, "reason": "No snapshot exists yet."}, ensure_ascii=False, indent=2))
        return 1
    payload = read_json(latest)
    matched = _query_records(payload.get("records", []), field=args.field, value=args.value, mode=args.mode)
    result = {
        "snapshot_path": str(latest),
        "matched_count": len(matched),
        "items": matched[: args.limit],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    latest = _latest_snapshot_path(args.source)
    previous = _previous_snapshot_path(args.source)
    if latest is None or previous is None:
        print(json.dumps({"ok": False, "reason": "Need at least two snapshots to compare."}, ensure_ascii=False, indent=2))
        return 1
    latest_payload = read_json(latest)
    previous_payload = read_json(previous)
    latest_ids = {str(item.get("accession") or item.get("uid")) for item in latest_payload.get("records", [])}
    previous_ids = {str(item.get("accession") or item.get("uid")) for item in previous_payload.get("records", [])}
    result = {
        "latest_snapshot": str(latest),
        "previous_snapshot": str(previous),
        "record_count_delta": len(latest_ids) - len(previous_ids),
        "new_accessions": sorted(latest_ids - previous_ids),
        "removed_accessions": sorted(previous_ids - latest_ids),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    latest = _latest_snapshot_path(args.source)
    if latest is None:
        print(json.dumps({"ok": False, "reason": "No snapshot exists yet."}, ensure_ascii=False, indent=2))
        return 1
    payload = read_json(latest)
    issues = _quality_issues(payload.get("records", []))
    result = {"snapshot_path": str(latest), "issue_count": len(issues), "issues": issues[: args.limit]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_query_local_db(args: argparse.Namespace) -> int:
    mysql_user = os.environ.get("DGA_MYSQL_USER")
    mysql_password = os.environ.get("DGA_MYSQL_PASSWORD")
    mysql_database = os.environ.get("DGA_MYSQL_DATABASE")
    mysql_host = os.environ.get("DGA_MYSQL_HOST", "localhost")
    if not all([mysql_user, mysql_password, mysql_database]):
        print(json.dumps({"ok": False, "reason": "Local DB is not configured via DGA_MYSQL_* env vars."}, ensure_ascii=False, indent=2))
        return 1
    cmd = [
        "mysql",
        f"-h{mysql_host}",
        f"-u{mysql_user}",
        f"-p{mysql_password}",
        mysql_database,
        "-e",
        args.sql,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload = {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal local governance helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    source_brief = subparsers.add_parser("source-brief", help="Show source governance brief.")
    source_brief.add_argument("--source", required=True, choices=sorted(SOURCE_REGISTRY))
    source_brief.set_defaults(func=cmd_source_brief)

    refresh = subparsers.add_parser("refresh", help="Refresh or materialize a latest snapshot.")
    refresh.add_argument("--source", required=True, choices=sorted(SOURCE_REGISTRY))
    refresh.add_argument("--limit", type=int, default=5)
    refresh.add_argument("--query")
    refresh.set_defaults(func=cmd_refresh)

    query_latest = subparsers.add_parser("query-latest", help="Query the latest snapshot.")
    query_latest.add_argument("--source", required=True, choices=sorted(SOURCE_REGISTRY))
    query_latest.add_argument("--field", required=True)
    query_latest.add_argument("--value", required=True)
    query_latest.add_argument("--mode", choices=("contains", "exact"), default="contains")
    query_latest.add_argument("--limit", type=int, default=20)
    query_latest.set_defaults(func=cmd_query_latest)

    compare = subparsers.add_parser("compare", help="Compare latest and previous snapshots.")
    compare.add_argument("--source", required=True, choices=sorted(SOURCE_REGISTRY))
    compare.set_defaults(func=cmd_compare)

    quality = subparsers.add_parser("quality", help="Inspect quality issues in latest snapshot.")
    quality.add_argument("--source", required=True, choices=sorted(SOURCE_REGISTRY))
    quality.add_argument("--limit", type=int, default=20)
    quality.set_defaults(func=cmd_quality)

    local_db = subparsers.add_parser("query-local-db", help="Run a read-only SQL query against a configured local DB.")
    local_db.add_argument("--sql", required=True)
    local_db.set_defaults(func=cmd_query_local_db)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
