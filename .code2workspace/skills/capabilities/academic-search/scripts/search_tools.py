#!/usr/bin/env python3
import requests
import json
import argparse
import re
import sys
import os
import xml.etree.ElementTree as ET
from pathlib import Path

# API Keys
PUBMED_API_KEY = "61fe6c7acfd07fc679cad91219b7d8216f09"
SPRINGER_OA_KEY = "0c6faca94c97955b8c6f984e1b1185a1"

DEFAULT_MAX_RESULTS = 5
SESSIONS_INDEX = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"


def node_text(node):
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def parse_abstract(article):
    parts = []
    for abstract in article.findall(".//AbstractText"):
        text = node_text(abstract)
        if not text:
            continue
        label = abstract.attrib.get("Label")
        parts.append(f"{label}: {text}" if label else text)
    return "\n".join(parts)


def parse_pubmed_article(article):
    pmid = node_text(article.find(".//PMID"))
    journal = node_text(article.find(".//Journal/Title")) or node_text(article.find(".//ISOAbbreviation"))
    doi = ""
    for article_id in article.findall(".//ArticleId"):
        if article_id.attrib.get("IdType") == "doi":
            doi = node_text(article_id)
            break
    authors = []
    for author in article.findall(".//Author"):
        collective = node_text(author.find("CollectiveName"))
        if collective:
            authors.append(collective)
            continue
        last = node_text(author.find("LastName"))
        fore = node_text(author.find("ForeName")) or node_text(author.find("Initials"))
        name = " ".join(part for part in [fore, last] if part)
        if name:
            authors.append(name)
    return {
        "pmid": pmid,
        "title": node_text(article.find(".//ArticleTitle")),
        "journal": journal,
        "pubdate": parse_pubmed_pubdate(article),
        "doi": doi,
        "authors": authors,
        "abstract": parse_abstract(article),
        "link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
    }


def parse_pubmed_pubdate(article):
    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        return ""
    medline = node_text(pub_date.find("MedlineDate"))
    if medline:
        return medline
    parts = [node_text(pub_date.find(tag)) for tag in ("Year", "Month", "Day")]
    return " ".join(part for part in parts if part)


def fetch_pubmed_details(pmids):
    if not pmids:
        return []
    sum_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml", "api_key": PUBMED_API_KEY}
    resp = get_with_session_fallback(sum_url, params=params, timeout=15, prefer_direct=True)
    root = ET.fromstring(resp.text)
    parsed = [parse_pubmed_article(article) for article in root.findall(".//PubmedArticle")]
    by_pmid = {item.get("pmid"): item for item in parsed}
    return [by_pmid[pmid] for pmid in pmids if pmid in by_pmid]


def springer_abstract(record):
    abstract = record.get("abstract", "")
    if isinstance(abstract, dict):
        texts = []
        for value in abstract.values():
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, list):
                texts.extend(str(item) for item in value)
        return "\n".join(texts)
    return str(abstract) if abstract else ""


def springer_url(record):
    urls = record.get("url") or []
    if isinstance(urls, list):
        for item in urls:
            if isinstance(item, dict) and item.get("value"):
                return item["value"]
            if isinstance(item, str):
                return item
    return ""


def build_session():
    session = requests.Session()
    session.trust_env = False

    proxies = {}
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    if proxies:
        session.proxies.update(proxies)
    return session


def build_direct_session():
    session = requests.Session()
    session.trust_env = False
    return session


def get_with_session_fallback(url, *, params=None, timeout=10, prefer_direct=False):
    sessions = [build_direct_session(), build_session()] if prefer_direct else [build_session(), build_direct_session()]
    last_error = None
    for session in sessions:
        try:
            resp = session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_error = exc
    raise last_error


def infer_max_results_from_recent_request():
    try:
        with SESSIONS_INDEX.open("r", encoding="utf-8") as handle:
            sessions = json.load(handle)
        session_file = sessions.get("agent:main:main", {}).get("sessionFile")
        candidates = []
        if session_file:
            candidates.append(Path(session_file))
        sessions_dir = SESSIONS_INDEX.parent
        if sessions_dir.exists():
            candidates.extend(sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True))
        seen = set()
        patterns = [
            r"只返回\s*(\d+)",
            r"返回\s*(\d+)\s*篇",
            r"(\d+)\s*篇论文",
            r"top\s*(\d+)",
            r"(\d+)\s*(?:papers|results)",
        ]
        for candidate in candidates:
            if candidate in seen or not candidate.exists():
                continue
            seen.add(candidate)
            with candidate.open("r", encoding="utf-8") as handle:
                for raw_line in reversed(handle.readlines()[-40:]):
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    message = payload.get("message", {})
                    if payload.get("type") != "message" or message.get("role") != "user":
                        continue
                    parts = message.get("content", [])
                    text = "\n".join(part.get("text", "") for part in parts if part.get("type") == "text")
                    for pattern in patterns:
                        match = re.search(pattern, text, flags=re.IGNORECASE)
                        if match:
                            value = int(match.group(1))
                            if value > 0:
                                return value
    except Exception:
        return None
    return None


def resolve_max_results(value):
    if value is not None:
        return value
    inferred = infer_max_results_from_recent_request()
    if inferred is not None:
        return inferred
    return DEFAULT_MAX_RESULTS

def search_pubmed(query, max_results=5, details=False):
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {"db": "pubmed", "term": query, "retmode": "json", "retmax": max_results, "api_key": PUBMED_API_KEY}
    try:
        resp = get_with_session_fallback(url, params=params, timeout=10, prefer_direct=True)
        id_list = resp.json().get("esearchresult", {}).get("idlist", [])
        if not id_list: return json.dumps({"error": "No papers found in PubMed."})
        if details:
            return json.dumps(fetch_pubmed_details(id_list), ensure_ascii=False, indent=2)
            
        sum_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        sum_params = {"db": "pubmed", "id": ",".join(id_list), "retmode": "json", "api_key": PUBMED_API_KEY}
        sum_resp = get_with_session_fallback(sum_url, params=sum_params, timeout=10, prefer_direct=True)
        summary_data = sum_resp.json().get("result", {})
        
        results = []
        for uid in id_list:
            if uid in summary_data:
                paper = summary_data[uid]
                results.append({
                    "pmid": uid,
                    "title": paper.get("title", ""),
                    "pubdate": paper.get("pubdate", ""),
                    "link": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/"
                })
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def search_springer(query, max_results=5, details=False):
    url = "https://api.springernature.com/openaccess/json"
    params = {"api_key": SPRINGER_OA_KEY, "q": query, "p": max_results}
    try:
        resp = get_with_session_fallback(url, params=params, timeout=10)
        records = resp.json().get("records", [])
        if not records: return json.dumps({"error": "No papers found in Springer."})

        if details:
            results = [
                {
                    "title": r.get("title", ""),
                    "date": r.get("publicationDate", ""),
                    "journal": r.get("publicationName", ""),
                    "doi": r.get("doi", ""),
                    "authors": [
                        creator.get("creator", "")
                        for creator in r.get("creators", [])
                        if isinstance(creator, dict) and creator.get("creator")
                    ],
                    "abstract": springer_abstract(r),
                    "link": springer_url(r),
                }
                for r in records
            ]
        else:
            results = [{"title": r.get("title", ""), "date": r.get("publicationDate", ""), "doi": r.get("doi", "")} for r in records]
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

def search_biorxiv(start_date, end_date, max_results=5, details=False):
    url = f"https://api.biorxiv.org/details/biorxiv/{start_date}/{end_date}"
    try:
        resp = get_with_session_fallback(url, timeout=15)
        collection = resp.json().get("collection", [])
        if not collection: return json.dumps({"error": "No preprints found."})

        if details:
            results = [
                {
                    "title": p.get("title", ""),
                    "date": p.get("date", ""),
                    "doi": p.get("doi", ""),
                    "authors": p.get("authors", ""),
                    "category": p.get("category", ""),
                    "abstract": p.get("abstract", ""),
                    "jatsxml": p.get("jatsxml", ""),
                    "link": f"https://www.biorxiv.org/content/{p.get('doi', '')}v{p.get('version', '')}" if p.get("doi") else "",
                }
                for p in collection[:max_results]
            ]
        else:
            results = [{"title": p.get("title", ""), "date": p.get("date", ""), "doi": p.get("doi", "")} for p in collection[:max_results]]
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as e: return json.dumps({"error": str(e)})

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Academic Search CLI for OpenClaw")
    parser.add_argument("--source", choices=["pubmed", "springer", "biorxiv"], required=True, help="The database to search")
    parser.add_argument("--query", type=str, help="Search keywords (required for pubmed and springer)")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD (required for biorxiv)")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD (required for biorxiv)")
    parser.add_argument("--max-results", type=int, default=None, help="Maximum number of results to return")
    parser.add_argument("--details", action="store_true", help="Return richer details such as abstracts, authors, journal/category, DOI, and links when available")
    parser.add_argument("--include-abstracts", action="store_true", help="Alias for --details")
    
    args = parser.parse_args()
    
    max_results = resolve_max_results(args.max_results)
    details = args.details or args.include_abstracts

    if args.source == "pubmed":
        if not args.query: print(json.dumps({"error": "Missing --query"})); sys.exit(1)
        print(search_pubmed(args.query, max_results=max_results, details=details))
    elif args.source == "springer":
        if not args.query: print(json.dumps({"error": "Missing --query"})); sys.exit(1)
        print(search_springer(args.query, max_results=max_results, details=details))
    elif args.source == "biorxiv":
        if not args.start or not args.end: print(json.dumps({"error": "Missing --start or --end"})); sys.exit(1)
        print(search_biorxiv(args.start, args.end, max_results=max_results, details=details))
