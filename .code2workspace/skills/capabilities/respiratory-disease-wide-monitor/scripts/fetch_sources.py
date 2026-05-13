#!/usr/bin/env python3
"""Fetch a broad respiratory disease source table and probe one level deeper.

The source table is an .xlsx file. This script intentionally uses only the
standard library plus requests/bs4, because the OpenClaw environment does not
always have openpyxl installed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_SOURCE_TABLE = Path(__file__).resolve().parents[1] / "整理后的数据源表.xlsx"
DEFAULT_TIMEOUT = 12
DEFAULT_WORKERS = 8
MAX_HTML_BYTES = 1_500_000
USER_AGENT = "OpenClaw respiratory-disease-wide-monitor/1.0"
NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

LINK_KEYWORDS = [
    "2026",
    "2025",
    "latest",
    "update",
    "weekly",
    "monthly",
    "report",
    "surveillance",
    "bulletin",
    "respiratory",
    "influenza",
    "covid",
    "sars-cov-2",
    "rsv",
    "variant",
    "pdf",
    "最新",
    "近期",
    "每周",
    "周报",
    "月报",
    "报告",
    "监测",
    "疫情",
    "呼吸",
    "新冠",
    "流感",
    "合胞",
    "变异",
    "通报",
]

BAD_LINK_PATTERNS = [
    "javascript:",
    "mailto:",
    "tel:",
    "#",
    "facebook.com",
    "twitter.com",
    "x.com/share",
    "linkedin.com",
    "whatsapp",
    "/login",
    "/signin",
]

QUERY_PROFILES = {
    "variant": {
        "triggers": [
            "variant",
            "variants",
            "lineage",
            "mutation",
            "mutant",
            "escape",
            "evolution",
            "voi",
            "vum",
            "voc",
            "gisaid",
            "nextstrain",
            "cov-spectrum",
            "covinet",
            "变异",
            "突变",
            "毒株",
            "谱系",
            "序列",
            "进化",
            "抗体逃逸",
        ],
        "keywords": [
            "variant",
            "variants",
            "lineage",
            "mutation",
            "escape",
            "evolution",
            "gisaid",
            "nextstrain",
            "cov-spectrum",
            "covinet",
            "tag-ve",
            "evescape",
            "cov-abdab",
            "dms",
            "变异",
            "突变",
            "毒株",
            "谱系",
            "序列",
            "进化",
            "抗体逃逸",
        ],
    },
    "covid": {
        "triggers": ["covid", "sars-cov-2", "coronavirus", "新冠", "冠状病毒"],
        "keywords": ["covid", "sars-cov-2", "coronavirus", "新冠", "冠状病毒"],
    },
    "flu": {
        "triggers": ["influenza", "flu", "流感", "grippe"],
        "keywords": ["influenza", "flu", "流感", "grippe"],
    },
    "rsv": {
        "triggers": ["rsv", "respiratory syncytial", "合胞"],
        "keywords": ["rsv", "respiratory syncytial", "合胞"],
    },
    "respiratory": {
        "triggers": ["respiratory", "呼吸道", "呼吸"],
        "keywords": ["respiratory", "呼吸道", "呼吸"],
    },
    "wastewater": {
        "triggers": ["wastewater", "nwss", "废水", "污水"],
        "keywords": ["wastewater", "nwss", "废水", "污水"],
    },
}

REGION_PROFILES = {
    "asia": {
        "triggers": ["asia", "亚洲", "东亚", "南亚", "港澳台", "中国", "香港", "台湾", "日本", "印度", "马来西亚", "新加坡"],
        "keywords": [
            "china",
            "中国",
            "中疾控",
            "香港",
            "台湾",
            "india",
            "malaysia",
            "singapore",
            "bangladesh",
            "pakistan",
            "日本",
            "厚生",
            "niid",
            "mhlw",
            "jihs",
        ],
    },
    "europe": {
        "triggers": ["europe", "欧洲", "欧美", "欧盟", "eea", "英国", "德国"],
        "keywords": ["ecdc", "erviss", "uk", "gov.uk", "europe", "eea", "robert koch", "rki", "grippeweb", "德国", "英国"],
    },
    "americas": {
        "triggers": ["america", "americas", "美洲", "欧美", "美国", "北美", "南美", "加拿大", "巴西", "阿根廷"],
        "keywords": ["u.s. cdc", "cdc.gov", "canada", "brazil", "saude", "argentina", "美国", "加拿大", "阿根廷"],
    },
    "oceania": {
        "triggers": ["oceania", "澳洲", "澳大利亚", "australia", "nsw"],
        "keywords": ["australia", "australian", "nsw", "澳大利亚"],
    },
    "africa": {
        "triggers": ["africa", "非洲", "南非"],
        "keywords": ["africa", "afro.who", "south africa", "nicd", "南非", "非洲"],
    },
}

LINEAGE_STOPWORDS = {
    "CDC",
    "WHO",
    "RSV",
    "COVID",
    "SARS",
    "PDF",
    "USA",
    "UK",
    "EU",
    "ICU",
    "ED",
}


@dataclass
class Source:
    index: int
    group: str
    name: str
    category: str
    maintainer: str
    url: str
    pathogen: str
    description: str


def col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    number = 0
    for ch in letters:
        number = number * 26 + (ord(ch.upper()) - 64)
    return number - 1


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    value = cell.find("main:v", NS)
    if value is None:
        return ""
    raw = value.text or ""
    if cell.attrib.get("t") == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def read_xlsx_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in shared_root.findall("main:si", NS):
            parts = [node.text or "" for node in item.findall(".//main:t", NS)]
            shared_strings.append("".join(parts))

        sheet_root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows: list[list[str]] = []
        for row in sheet_root.findall(".//main:sheetData/main:row", NS):
            values: dict[int, str] = {}
            max_col = -1
            for cell in row.findall("main:c", NS):
                idx = col_index(cell.attrib["r"])
                max_col = max(max_col, idx)
                values[idx] = cell_text(cell, shared_strings).strip()
            rows.append([values.get(i, "") for i in range(max_col + 1)])
    return rows


def load_sources(path: Path) -> list[Source]:
    rows = read_xlsx_rows(path)
    sources: list[Source] = []
    group = ""
    for row_number, row in enumerate(rows[1:], start=2):
        padded = [*row, "", "", "", "", "", ""]
        name, category, maintainer, url, pathogen, description = padded[:6]
        if category == "站点名称" and not url:
            group = name
            continue
        if not url.startswith(("http://", "https://")):
            continue
        sources.append(
            Source(
                index=row_number,
                group=group,
                name=name,
                category=category,
                maintainer=maintainer,
                url=url,
                pathogen=pathogen,
                description=description,
            )
        )
    return sources


def clean_text(raw: str) -> str:
    return re.sub(r"\s+", " ", unescape(raw or "")).strip()


def trim(value: str, limit: int = 800) -> str:
    value = clean_text(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def source_text(source: Source) -> str:
    return " ".join(
        [
            source.group,
            source.name,
            source.category,
            source.maintainer,
            source.pathogen,
            source.description,
            source.url,
        ]
    ).casefold()


def query_terms(query: str) -> list[str]:
    return [term for term in re.split(r"\s+", query.strip()) if term]


def looks_like_variant_lineage(term: str) -> bool:
    token = term.strip(" ,;:()[]{}，。；：（）【】")
    if token.upper() in LINEAGE_STOPWORDS:
        return False
    return bool(re.fullmatch(r"[A-Z]{1,3}(?:\.[0-9A-Z]+){0,4}", token))


def infer_query_profiles(query: str) -> set[str]:
    lowered = query.casefold()
    profiles: set[str] = set()
    for name, profile in QUERY_PROFILES.items():
        if any(trigger.casefold() in lowered for trigger in profile["triggers"]):
            profiles.add(name)
    if any(looks_like_variant_lineage(term) for term in query_terms(query)):
        profiles.add("variant")
    return profiles


def infer_region_profiles(query: str) -> set[str]:
    lowered = query.casefold()
    regions: set[str] = set()
    for name, profile in REGION_PROFILES.items():
        if any(trigger.casefold() in lowered for trigger in profile["triggers"]):
            regions.add(name)
    return regions


def score_source_for_query(source: Source, query: str) -> int:
    text = source_text(source)
    terms = [term.casefold() for term in query_terms(query)]
    profiles = infer_query_profiles(query)
    regions = infer_region_profiles(query)
    score = 0

    if terms and all(term in text for term in terms):
        score += 100
    for term in terms:
        if term in text:
            score += 10

    for profile_name in profiles:
        profile = QUERY_PROFILES[profile_name]
        for keyword in profile["keywords"]:
            if keyword.casefold() in text:
                score += 6

        if profile_name == "variant":
            high_signal = any(
                keyword in text
                for keyword in [
                    "variant",
                    "variants",
                    "lineage",
                    "gisaid",
                    "nextstrain",
                    "cov-spectrum",
                    "covinet",
                    "tag-ve",
                    "evescape",
                    "cov-abdab",
                    "dms",
                    "变异",
                    "突变",
                    "毒株",
                    "谱系",
                    "序列",
                ]
            )
            if source.category == "突变株":
                score += 20
            if high_signal:
                score += 40
            if high_signal and source.maintainer in {"WHO", "U.S. CDC", "ECDC", "GISAID"}:
                score += 60
            if source.pathogen in {"新冠", "所有病原"}:
                score += 10
        elif profile_name == "covid":
            if source.pathogen in {"新冠", "所有病原"}:
                score += 30
        elif profile_name in {"flu", "rsv", "respiratory"}:
            if source.pathogen == "所有病原":
                score += 25
        elif profile_name == "wastewater":
            if "wastewater" in text or "nwss" in text:
                score += 50

    for region_name in regions:
        if any(keyword.casefold() in text for keyword in REGION_PROFILES[region_name]["keywords"]):
            score += 70

    return score


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def read_response_bytes(response: requests.Response, max_bytes: int = MAX_HTML_BYTES) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        remaining = max_bytes - total
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        total += len(chunks[-1])
    return b"".join(chunks)


def decode_response(response: requests.Response, content: bytes) -> str:
    candidates: list[str] = []
    encoding = response.encoding
    if encoding and encoding.lower() not in {"iso-8859-1", "latin-1"}:
        candidates.append(encoding)
    candidates.extend(["utf-8", "gb18030", "big5", "shift_jis", "latin-1"])

    best_text = ""
    best_score = 10**9
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            text = content.decode(candidate, errors="replace")
        except LookupError:
            continue
        score = text.count("\ufffd") * 10 + text.count("Ã") + text.count("å") + text.count("ç")
        if score < best_score:
            best_score = score
            best_text = text
    return best_text


def parse_html(url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.get_text(" ")) if soup.title else ""

    description = ""
    for selector in [
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"},
    ]:
        tag = soup.find("meta", attrs=selector)
        if tag and tag.get("content"):
            description = clean_text(str(tag["content"]))
            break

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body_text = trim(soup.get_text(" "), 1000)

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = clean_text(str(anchor.get("href", "")))
        if not href:
            continue
        absolute = urljoin(url, href)
        lowered = absolute.lower()
        if any(pattern in lowered for pattern in BAD_LINK_PATTERNS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        text = trim(anchor.get_text(" "), 160)
        links.append({"url": absolute, "text": text})
    return {"title": title, "description": description, "snippet": body_text, "links": links}


def link_score(parent_url: str, link: dict[str, str]) -> int:
    href = link["url"]
    text = f"{link.get('text', '')} {href}".casefold()
    score = 0
    for keyword in LINK_KEYWORDS:
        if keyword.casefold() in text:
            score += 3
    parent_host = urlparse(parent_url).netloc
    child_host = urlparse(href).netloc
    if parent_host and child_host == parent_host:
        score += 4
    if href.lower().split("?", 1)[0].endswith(".pdf"):
        score += 5
    if re.search(r"20(2[5-9]|3\d)", text):
        score += 2
    return score


def choose_child_links(parent_url: str, links: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    if limit <= 0:
        return []
    scored = []
    for link in links:
        score = link_score(parent_url, link)
        if score > 0:
            scored.append((score, link))
    scored.sort(key=lambda item: (-item[0], item[1]["url"]))
    return [link for _, link in scored[:limit]]


def fetch_url(session: requests.Session, url: str, timeout: int) -> dict[str, Any]:
    started = time.time()
    result: dict[str, Any] = {"url": url}
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
        content = read_response_bytes(response)
        result.update(
            {
                "ok": bool(response.ok),
                "status_code": response.status_code,
                "final_url": response.url,
                "content_type": response.headers.get("content-type", ""),
                "content_length": response.headers.get("content-length", ""),
                "sampled_bytes": len(content),
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        )
        if not response.ok:
            result["error"] = f"HTTP {response.status_code}"
            return result

        content_type = result["content_type"].lower()
        final_path = urlparse(response.url).path.lower()
        if "pdf" in content_type or final_path.endswith(".pdf"):
            result["kind"] = "pdf"
            result["title"] = Path(final_path).name or "PDF"
            result["snippet"] = "PDF reachable; only metadata and an initial byte sample were fetched."
            return result

        html = decode_response(response, content)
        parsed = parse_html(response.url, html)
        result.update(
            {
                "kind": "html",
                "title": parsed["title"],
                "description": parsed["description"],
                "snippet": parsed["snippet"],
                "links": parsed["links"],
            }
        )
        return result
    except requests.RequestException as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            try:
                requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
                response = session.get(url, timeout=timeout, allow_redirects=True, stream=True, verify=False)
                content = read_response_bytes(response)
                result.update(
                    {
                        "ok": bool(response.ok),
                        "status_code": response.status_code,
                        "final_url": response.url,
                        "content_type": response.headers.get("content-type", ""),
                        "content_length": response.headers.get("content-length", ""),
                        "sampled_bytes": len(content),
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "warning": "TLS certificate verification failed; retried with verify=False.",
                    }
                )
                if not response.ok:
                    result["error"] = f"HTTP {response.status_code}"
                    return result
                content_type = result["content_type"].lower()
                final_path = urlparse(response.url).path.lower()
                if "pdf" in content_type or final_path.endswith(".pdf"):
                    result.update(
                        {
                            "kind": "pdf",
                            "title": Path(final_path).name or "PDF",
                            "snippet": "PDF reachable; only metadata and an initial byte sample were fetched.",
                        }
                    )
                    return result
                parsed = parse_html(response.url, decode_response(response, content))
                result.update(
                    {
                        "kind": "html",
                        "title": parsed["title"],
                        "description": parsed["description"],
                        "snippet": parsed["snippet"],
                        "links": parsed["links"],
                    }
                )
                return result
            except requests.RequestException as retry_exc:
                exc = retry_exc
        result.update({"ok": False, "error": str(exc), "elapsed_ms": int((time.time() - started) * 1000)})
        return result


def probe_source(source: Source, *, child_limit: int, timeout: int) -> dict[str, Any]:
    session = build_session()
    root = fetch_url(session, source.url, timeout)
    children: list[dict[str, Any]] = []
    for child_link in choose_child_links(source.url, root.get("links", []), child_limit):
        child = fetch_url(session, child_link["url"], timeout)
        child["anchor_text"] = child_link.get("text", "")
        child.pop("links", None)
        children.append(child)

    root.pop("links", None)
    return {
        "source": {
            "index": source.index,
            "group": source.group,
            "name": source.name,
            "category": source.category,
            "maintainer": source.maintainer,
            "url": source.url,
            "pathogen": source.pathogen,
            "description": source.description,
        },
        "root": root,
        "children": children,
    }


def filter_sources(
    sources: list[Source],
    *,
    query: str | None,
    category: str | None,
    pathogen: str | None,
    limit: int,
) -> list[Source]:
    filtered = sources
    if category:
        filtered = [source for source in filtered if category.casefold() in source.category.casefold()]
    if pathogen:
        filtered = [
            source
            for source in filtered
            if pathogen.casefold() in source.pathogen.casefold() or source.pathogen == "所有病原"
        ]
    if query:
        scored = [(score_source_for_query(source, query), source) for source in filtered]
        scored = [(score, source) for score, source in scored if score > 0]
        scored.sort(key=lambda item: (-item[0], item[1].index))
        filtered = [source for _, source in scored]
    if limit > 0:
        filtered = filtered[:limit]
    return filtered


def to_markdown(results: list[dict[str, Any]], table_path: Path) -> str:
    ok_count = sum(1 for item in results if item["root"].get("ok"))
    child_count = sum(len(item.get("children", [])) for item in results)
    lines = [
        "# 广域呼吸道病原数据源监测",
        "",
        f"- 数据源表: `{table_path}`",
        f"- 数据源数: {len(results)}",
        f"- 根页面可访问: {ok_count}/{len(results)}",
        f"- 下探页面/PDF: {child_count}",
        "",
    ]
    for item in results:
        source = item["source"]
        root = item["root"]
        lines.extend(
            [
                f"## {source['name']}",
                f"- 分组: {source.get('group') or 'N/A'}",
                f"- 维护方: {source.get('maintainer') or 'N/A'}",
                f"- 病原类型: {source.get('pathogen') or 'N/A'}",
                f"- 根页面: {source['url']}",
                f"- 根页面状态: {root.get('status_code', 'ERR')} {root.get('content_type', '')}",
            ]
        )
        if root.get("title"):
            lines.append(f"- 根页面标题: {root['title']}")
        if root.get("description"):
            lines.append(f"- 根页面描述: {trim(root['description'], 240)}")
        if root.get("error"):
            lines.append(f"- 错误: {root['error']}")
        for child in item.get("children", []):
            label = child.get("anchor_text") or child.get("title") or child.get("url")
            lines.append(f"- 下探: {label}")
            lines.append(f"  - URL: {child.get('final_url') or child.get('url')}")
            lines.append(f"  - 状态: {child.get('status_code', 'ERR')} {child.get('content_type', '')}")
            if child.get("title"):
                lines.append(f"  - 标题: {child['title']}")
            if child.get("description"):
                lines.append(f"  - 描述: {trim(child['description'], 240)}")
            if child.get("error"):
                lines.append(f"  - 错误: {child['error']}")
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch 50+ respiratory disease monitoring sources from an XLSX table.")
    parser.add_argument("--source-table", type=Path, default=DEFAULT_SOURCE_TABLE, help="XLSX data source table")
    parser.add_argument("--query", help="Filter by keyword across source metadata")
    parser.add_argument("--category", help="Filter by data source category")
    parser.add_argument("--pathogen", help="Filter by pathogen type, e.g. 新冠, 流感, RSV")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of sources; 0 means all")
    parser.add_argument("--child-limit", type=int, default=1, help="Fetch this many likely child pages/PDFs per source")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-request timeout seconds")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Concurrent fetch workers")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--output", type=Path, help="Optional output file path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sources = filter_sources(
        load_sources(args.source_table),
        query=args.query,
        category=args.category,
        pathogen=args.pathogen,
        limit=args.limit,
    )
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(probe_source, source, child_limit=args.child_limit, timeout=args.timeout): source
            for source in sources
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                source = futures[future]
                results.append(
                    {
                        "source": source.__dict__,
                        "root": {"url": source.url, "ok": False, "error": f"probe failed: {exc}"},
                        "children": [],
                    }
                )
    results.sort(key=lambda item: int(item["source"]["index"]))

    if args.format == "json":
        payload = json.dumps(
            {
                "source_table": str(args.source_table),
                "source_count": len(results),
                "root_ok_count": sum(1 for item in results if item["root"].get("ok")),
                "child_count": sum(len(item.get("children", [])) for item in results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        payload = to_markdown(results, args.source_table)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(str(args.output))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
