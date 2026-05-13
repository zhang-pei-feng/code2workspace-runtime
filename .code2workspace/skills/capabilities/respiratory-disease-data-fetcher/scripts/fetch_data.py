#!/usr/bin/env python3
import argparse
import json
import re
from html import unescape
from urllib.parse import urljoin

import requests

TIMEOUT_SECONDS = 20
MAX_LINKS = 5
USER_AGENT = "OpenClaw respiratory-disease-data-fetcher/1.0"

SOURCES = {
    "who_cases": {
        "label": "WHO COVID-19 Cases Dashboard",
        "url": "https://data.who.int/dashboards/covid19/cases",
    },
    "us_cdc_trends": {
        "label": "US CDC COVID Data Tracker",
        "url": "https://covid.cdc.gov/covid-data-tracker/",
    },
    "china_cdc": {
        "label": "China CDC Respiratory Disease Updates",
        "url": "https://www.chinacdc.cn/jksj/xgbdyq/",
    },
    "who_africa_updates": {
        "label": "WHO Africa Outbreak Updates",
        "url": "https://www.afro.who.int/health-topics/disease-outbreaks/outbreaks-and-other-emergencies-updates",
    },
    "who_variants": {
        "label": "WHO COVID-19 Variants Dashboard",
        "url": "https://data.who.int/dashboards/covid19/variants",
    },
}


def build_session():
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def clean_text(raw):
    return re.sub(r"\s+", " ", unescape(raw or "")).strip()


def extract_title(html):
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    return clean_text(match.group(1)) if match else ""


def extract_description(html):
    patterns = [
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return clean_text(match.group(1))
    return ""


def extract_links(html, limit=MAX_LINKS):
    links = []
    seen = set()
    for href, text in re.findall(r'<a[^>]+href=["\'](.*?)["\'][^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL):
        href = clean_text(href)
        text = clean_text(re.sub(r"<[^>]+>", " ", text))
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        key = (href, text)
        if key in seen:
            continue
        seen.add(key)
        links.append({"text": text, "href": href})
        if len(links) >= limit:
            break
    return links


def strip_tags(html):
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return clean_text(text)


def extract_china_cdc_monthly_reports(html):
    reports = []
    pattern = re.compile(
        r'<a href="(?P<href>\./\d+/t\d+_\d+\.html)"[^>]*>(?P<title>全国新型冠状病毒感染疫情情况（(?P<month>[^）]+)）)<span>(?P<date>\d{4}-\d{2}-\d{2})</span></a>\s*<p class="zy">\s*(?P<summary>.*?)</p>',
        flags=re.DOTALL,
    )
    for match in pattern.finditer(html):
        reports.append(
            {
                "title": clean_text(match.group("title")),
                "month": clean_text(match.group("month")),
                "published_at": match.group("date"),
                "href": match.group("href"),
                "summary": strip_tags(match.group("summary")),
            }
        )
    return reports


def fetch_text(session, url):
    response = session.get(url, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text or ""


def search_first(pattern, text):
    match = re.search(pattern, text, flags=re.DOTALL)
    return clean_text(match.group(1)) if match else None


def search_all(pattern, text):
    return [clean_text(item) for item in re.findall(pattern, text, flags=re.DOTALL)]


def enrich_china_cdc_result(session, result, html):
    reports = extract_china_cdc_monthly_reports(html)
    if reports:
        result["monthly_reports"] = reports[:6]
    if not reports:
        return result

    latest = reports[0].copy()
    latest["url"] = urljoin(result["final_url"], latest.pop("href"))

    try:
        detail_html = fetch_text(session, latest["url"])
        detail_text = strip_tags(detail_html)

        latest["period"] = search_first(r"(20\d{2}年\d{1,2}月\d{1,2}日-\d{1,2}月\d{1,2}日)", detail_text)
        latest["fever_clinic_trend"] = search_first(
            r"发热门诊（诊室）诊疗量，(.*?)见图1",
            detail_text,
        )
        latest["confirmed_cases"] = search_first(r"新增确诊病例(\d+)例", detail_text)
        latest["severe_cases"] = search_first(r"其中重症病例(\d+)例", detail_text)
        latest["death_cases"] = search_first(r"死亡病例(\d+)例", detail_text)
        latest["main_variant"] = search_first(r"主要流行株为([^。]+)", detail_text)

        weekly_shares = search_all(r"占比分别为([0-9.%、]+)", detail_text)
        if weekly_shares:
            latest["variant_share_series"] = weekly_shares[0]

        lineages = search_all(r"\b([A-Z]{1,4}(?:\.[A-Z0-9]+)+)\b", detail_text)
        if lineages:
            seen = []
            for item in lineages:
                if item not in seen:
                    seen.append(item)
            latest["mentioned_lineages"] = seen[:10]
    except requests.RequestException as exc:
        latest["detail_error"] = str(exc)

    result["latest_report"] = latest
    return result


def fetch_page(session, name, config):
    result = {
        "source": name,
        "label": config["label"],
        "url": config["url"],
    }
    try:
        response = session.get(config["url"], timeout=TIMEOUT_SECONDS)
        result["status_code"] = response.status_code
        result["final_url"] = response.url
        result["content_type"] = response.headers.get("content-type")

        if not response.ok:
            result["error"] = f"HTTP {response.status_code}"
            return result

        text = response.text or ""
        result["title"] = extract_title(text)
        description = extract_description(text)
        if description:
            result["description"] = description
        links = extract_links(text)
        if links:
            result["links"] = links
        if name == "china_cdc":
            enrich_china_cdc_result(session, result, text)
        if not result.get("title") and not result.get("description") and not result.get("links"):
            snippet = clean_text(text[:400])
            result["snippet"] = snippet
        return result
    except requests.RequestException as exc:
        result["error"] = str(exc)
        return result


def fetch_data(source=None):
    session = build_session()
    if source:
        config = SOURCES[source]
        return {source: fetch_page(session, source, config)}
    return {name: fetch_page(session, name, config) for name, config in SOURCES.items()}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=sorted(SOURCES.keys()))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(fetch_data(source=args.source), ensure_ascii=False, indent=2))
