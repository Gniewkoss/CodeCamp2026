from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

GDELT_DOC_URL = "http://api.gdeltproject.org/api/v2/doc/doc"

RSS_FEEDS = [
    ("pb.pl", "https://www.pb.pl/rss/rss.xml"),
    ("bankier.pl", "https://www.bankier.pl/rss/wiadomosci.xml"),
    ("money.pl", "https://www.money.pl/rss/wiadomosci.xml"),
    ("wyborcza.biz", "https://wyborcza.biz/rss.xml"),
]


@dataclass
class RawArticle:
    url: str
    title: str | None
    source: str
    published_at: datetime | None
    language: str | None
    summary: str | None = None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def fetch_gdelt(company_name: str, max_records: int = 50) -> list[RawArticle]:
    settings = get_settings()
    if not settings.gdelt_enabled:
        return []
    params = {
        "query": company_name,
        "mode": "artlist",
        "maxrecords": str(max_records),
        "format": "json",
        "timespan": "MONTH",
    }
    out: list[RawArticle] = []
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.get(GDELT_DOC_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("GDELT error: %s", e)
        return []
    arts = data.get("articles") or data.get("article") or []
    if isinstance(arts, dict):
        arts = [arts]
    for row in arts:
        url = row.get("url") or row.get("URL")
        if not url:
            continue
        title = row.get("title") or row.get("Title")
        domain = row.get("domain") or row.get("sourceDomain") or urlparse(url).netloc
        seendate = row.get("seendate") or row.get("seen")
        lang = row.get("language") or row.get("lang")
        pub = _parse_dt(seendate)
        out.append(
            RawArticle(
                url=str(url),
                title=str(title) if title else None,
                source="gdelt",
                published_at=pub,
                language=str(lang)[:8] if lang else None,
            )
        )
    return out


def fetch_newsapi(company_name: str, page_size: int = 50) -> list[RawArticle]:
    settings = get_settings()
    key = settings.newsapi_key
    if not key:
        return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": company_name,
        "language": "pl",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": key,
    }
    out: list[RawArticle] = []
    try:
        with httpx.Client(timeout=45.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("NewsAPI error: %s", e)
        return []
    for art in data.get("articles") or []:
        u = art.get("url")
        if not u:
            continue
        dom = urlparse(u).netloc.lower().replace("www.", "")
        out.append(
            RawArticle(
                url=str(u),
                title=art.get("title"),
                source=dom or "newsapi",
                published_at=_parse_dt(art.get("publishedAt")),
                language="pl",
                summary=art.get("description"),
            )
        )
    return out


def _matches_company(text: str | None, name: str, aliases: list[str] | None) -> bool:
    if not text:
        return False
    blob = text.lower()
    needles = [name.lower()]
    if aliases:
        needles.extend(a.lower() for a in aliases if a)
    return any(n in blob for n in needles if len(n) >= 2)


def fetch_rss_filtered(company_name: str, aliases: list[str] | None = None) -> list[RawArticle]:
    out: list[RawArticle] = []
    for source_key, feed_url in RSS_FEEDS:
        try:
            with httpx.Client(timeout=45.0, headers={"User-Agent": "ReputationMonitor/1.0"}) as client:
                r = client.get(feed_url)
                r.raise_for_status()
                parsed = feedparser.parse(r.text)
        except Exception as e:
            logger.warning("RSS fetch failed %s: %s", feed_url, e)
            continue
        for entry in parsed.entries or []:
            title = entry.get("title")
            summary = entry.get("summary") or entry.get("description")
            link = entry.get("link")
            if not link:
                continue
            hay = f"{title or ''} {summary or ''}"
            if not _matches_company(hay, company_name, aliases):
                continue
            pub = _parse_dt(entry.get("published") or entry.get("updated"))
            out.append(
                RawArticle(
                    url=str(link),
                    title=str(title) if title else None,
                    source=source_key,
                    published_at=pub,
                    language="pl",
                    summary=str(summary)[:2000] if summary else None,
                )
            )
    return out


def collect_all_sources(company_name: str, aliases: list[str] | None) -> list[RawArticle]:
    seen: set[str] = set()
    merged: list[RawArticle] = []
    for batch in (
        fetch_rss_filtered(company_name, aliases),
        fetch_gdelt(company_name),
        fetch_newsapi(company_name),
    ):
        for a in batch:
            if a.url in seen:
                continue
            seen.add(a.url)
            merged.append(a)
    return merged
