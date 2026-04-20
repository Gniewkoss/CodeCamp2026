"""News source aggregation. RSS + NewsAPI + GDELT.

The scraper intentionally casts a wide net; final company disambiguation
is done by the Claude analyzer downstream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urlparse

import feedparser
import httpx
from rapidfuzz import fuzz

from app.config import get_settings

logger = logging.getLogger(__name__)

GDELT_DOC_URL = "http://api.gdeltproject.org/api/v2/doc/doc"

RSS_FEEDS: list[tuple[str, str]] = [
    ("pb.pl", "https://www.pb.pl/rss/rss.xml"),
    ("bankier.pl", "https://www.bankier.pl/rss/wiadomosci.xml"),
    ("money.pl", "https://www.money.pl/rss/wiadomosci.xml"),
    ("wyborcza.biz", "https://wyborcza.biz/rss.xml"),
    ("rp.pl", "https://www.rp.pl/rss/2"),
    ("businessinsider", "https://businessinsider.com.pl/.feed"),
    ("forsal", "https://forsal.pl/rss.xml"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 ReputationMonitor/2.0"
)


@dataclass
class RawArticle:
    url: str
    title: str | None
    source: str
    published_at: datetime | None
    language: str | None = "pl"
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


def _name_variants(name: str, aliases: Iterable[str] | None) -> list[str]:
    variants = {name.strip()}
    for a in aliases or []:
        a = (a or "").strip()
        if len(a) >= 2:
            variants.add(a)
    return [v for v in variants if v]


def _fuzzy_match(hay: str, needles: list[str], threshold: int = 80) -> bool:
    if not hay:
        return False
    blob = hay.lower()
    for n in needles:
        nl = n.lower()
        if nl in blob:
            return True
        if len(nl) >= 4 and fuzz.partial_ratio(nl, blob) >= threshold:
            return True
    return False


def fetch_newsapi(company_name: str, aliases: list[str] | None = None, page_size: int = 50) -> list[RawArticle]:
    settings = get_settings()
    if not settings.newsapi_key:
        return []
    url = "https://newsapi.org/v2/everything"
    variants = _name_variants(company_name, aliases)
    query = " OR ".join(f'"{v}"' for v in variants[:4]) or f'"{company_name}"'
    params = {
        "q": query,
        "language": "pl",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": settings.newsapi_key,
    }
    try:
        with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("NewsAPI error: %s", e)
        return []

    out: list[RawArticle] = []
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
                summary=art.get("description") or art.get("content"),
            )
        )
    return out


def fetch_gdelt(company_name: str, max_records: int = 40) -> list[RawArticle]:
    settings = get_settings()
    if not settings.gdelt_enabled:
        return []
    params = {
        "query": f'"{company_name}"',
        "mode": "artlist",
        "maxrecords": str(max_records),
        "format": "json",
        "timespan": "MONTH",
    }
    out: list[RawArticle] = []
    try:
        with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
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
        u = row.get("url") or row.get("URL")
        if not u:
            continue
        out.append(
            RawArticle(
                url=str(u),
                title=str(row.get("title") or row.get("Title") or "") or None,
                source=(row.get("domain") or urlparse(u).netloc).lower().replace("www.", "") or "gdelt",
                published_at=_parse_dt(row.get("seendate") or row.get("seen")),
                language=(row.get("language") or "pl"),
            )
        )
    return out


def fetch_rss(company_name: str, aliases: list[str] | None = None) -> list[RawArticle]:
    needles = _name_variants(company_name, aliases)
    if not needles:
        return []
    out: list[RawArticle] = []
    for source_key, feed_url in RSS_FEEDS:
        try:
            with httpx.Client(timeout=25.0, headers={"User-Agent": USER_AGENT}) as client:
                r = client.get(feed_url)
                r.raise_for_status()
                parsed = feedparser.parse(r.text)
        except Exception as e:
            logger.debug("RSS %s failed: %s", feed_url, e)
            continue
        for entry in parsed.entries or []:
            link = entry.get("link")
            if not link:
                continue
            title = entry.get("title") or ""
            summary = entry.get("summary") or entry.get("description") or ""
            haystack = f"{title} {summary}"
            if not _fuzzy_match(haystack, needles):
                continue
            out.append(
                RawArticle(
                    url=str(link),
                    title=str(title) or None,
                    source=source_key,
                    published_at=_parse_dt(entry.get("published") or entry.get("updated")),
                    language="pl",
                    summary=(summary[:2000] if summary else None),
                )
            )
    return out


def collect_all_sources(
    company_name: str,
    aliases: list[str] | None,
    *,
    limit: int | None = None,
) -> list[RawArticle]:
    seen: set[str] = set()
    merged: list[RawArticle] = []
    for batch in (
        fetch_newsapi(company_name, aliases),
        fetch_rss(company_name, aliases),
        fetch_gdelt(company_name),
    ):
        for a in batch:
            key = a.url.split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            merged.append(a)
    merged.sort(key=lambda a: a.published_at or datetime.fromtimestamp(0, tz=timezone.utc), reverse=True)
    if limit:
        merged = merged[:limit]
    return merged
