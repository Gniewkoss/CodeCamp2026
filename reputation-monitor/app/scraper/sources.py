"""News source aggregation. RSS + NewsAPI + GDELT.

The scraper intentionally casts a wide net; final company disambiguation
is done by the Claude analyzer downstream.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urlparse

import feedparser
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

GDELT_DOC_URL = "http://api.gdeltproject.org/api/v2/doc/doc"

# Once a NewsAPI key proves to be invalid (401) we stop retrying it for the
# rest of the process lifetime. Warnings are noisy and misleading on every
# scan otherwise.
_NEWSAPI_DISABLED: bool = False

RSS_FEEDS: list[tuple[str, str]] = [
    # Curated Polish business / general-news RSS. Only feeds verified to
    # return valid XML in 2026 — dead ones (rp.pl/rss, forsal.pl/feed.xml,
    # tvn24.pl/biznes.xml, parkiet.com/rss/3, polsatnews.pl/rss/biznes.xml)
    # were removed after repeatedly 404-ing.
    ("pb.pl",            "https://www.pb.pl/rss/najnowsze.xml"),
    ("bankier.pl",       "https://www.bankier.pl/rss/wiadomosci.xml"),
    ("money.pl",         "https://www.money.pl/rss/"),
    ("wyborcza.biz",     "https://rss.gazetaprawna.pl/rss/3"),
    ("businessinsider",  "https://businessinsider.com.pl/.feed"),
    ("wnp.pl",           "https://www.wnp.pl/rss/serwis_rss.xml"),
    ("interia.biznes",   "https://biznes.interia.pl/feed"),
    ("gazetaprawna",     "https://rss.gazetaprawna.pl/rss/6"),
    ("rp.pl",            "https://www.rp.pl/rss/3"),
    ("polsatnews.biznes","https://www.polsatnews.pl/rss/biznes.xml"),
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


def _strict_match(hay: str, needles: list[str]) -> bool:
    """Strict word-boundary match — no fuzzy/partial_ratio.

    Partial fuzz ratio on short brand names ("mbank", "orlen") gives massive
    false-positives: it matches any article that happens to contain the
    substring ("banki", "oren") in unrelated coverage. We therefore require
    the needle to appear as a *whole word* (Unicode-aware) in the haystack.
    """
    if not hay:
        return False
    blob = hay
    for n in needles:
        n = (n or "").strip()
        if len(n) < 2:
            continue
        # Word-boundary regex, Unicode-safe, case-insensitive.
        pattern = rf"(?<!\w){re.escape(n)}(?!\w)"
        if re.search(pattern, blob, flags=re.IGNORECASE | re.UNICODE):
            return True
    return False


def _fuzzy_match(hay: str, needles: list[str], threshold: int = 80) -> bool:  # backwards compat
    return _strict_match(hay, needles)


def fetch_newsapi(company_name: str, aliases: list[str] | None = None, page_size: int = 100) -> list[RawArticle]:
    """Query NewsAPI /everything with a time-bounded freshness window.

    Uses every meaningful alias (up to 6) joined with OR so that scans of
    "InPost" actually catch "Grupa InPost" and "InPost Paczkomaty" coverage.
    Adds ?from=today-N so re-scans surface fresh stories rather than the
    same backlog.
    """
    global _NEWSAPI_DISABLED
    settings = get_settings()
    if not settings.newsapi_key or _NEWSAPI_DISABLED:
        return []
    url = "https://newsapi.org/v2/everything"
    variants = _name_variants(company_name, aliases)
    query = " OR ".join(f'"{v}"' for v in variants[:6]) or f'"{company_name}"'
    since = datetime.now(timezone.utc) - timedelta(days=settings.news_lookback_days)
    params = {
        "q": query,
        "language": "pl",
        "sortBy": "publishedAt",
        "pageSize": min(page_size, 100),
        "from": since.strftime("%Y-%m-%d"),
        "apiKey": settings.newsapi_key,
    }
    try:
        with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
            r = client.get(url, params=params)
            # A 401 means the API key is invalid — disable for the session so
            # we don't spam 50 identical warnings per scan.
            if r.status_code == 401:
                _NEWSAPI_DISABLED = True
                logger.warning(
                    "NewsAPI: key rejected (401) — disabling NewsAPI for this "
                    "process. Update NEWSAPI_KEY in .env and restart."
                )
                return []
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
        title = art.get("title") or ""
        body = art.get("description") or art.get("content") or ""
        # Even NewsAPI sometimes returns loose matches — keep only those that
        # contain the company name as a proper word.
        if not _strict_match(f"{title} {body}", variants):
            continue
        dom = urlparse(u).netloc.lower().replace("www.", "")
        out.append(
            RawArticle(
                url=str(u),
                title=title or None,
                source=dom or "newsapi",
                published_at=_parse_dt(art.get("publishedAt")),
                language="pl",
                summary=body or None,
            )
        )
    logger.info("NewsAPI: %d articles for query %r (window %sd)", len(out), query[:120], settings.news_lookback_days)
    return out


def fetch_google_news(company_name: str, aliases: list[str] | None = None, limit: int = 80) -> list[RawArticle]:
    """Free, keyless news search via Google News RSS.

    Much more reliable than NewsAPI for Polish content when the NewsAPI key
    is missing / rate-limited (we just saw 401s for free-tier keys). Google
    News returns fresh, company-filtered coverage out of the box — we still
    post-filter with a strict word-boundary match against the aliases, so
    false positives never leak into the analysis pipeline.
    """
    variants = _name_variants(company_name, aliases)
    if not variants:
        return []
    # Quote every variant and OR them — Google News understands Boolean syntax.
    quoted = [f'"{v}"' for v in variants[:6]]
    query = " OR ".join(quoted)
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "pl", "gl": "PL", "ceid": "PL:pl"}
    try:
        with httpx.Client(timeout=25.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            parsed = feedparser.parse(r.text)
    except Exception as e:
        logger.warning("GoogleNews error: %s", e)
        return []
    out: list[RawArticle] = []
    for entry in (parsed.entries or [])[:limit]:
        link = entry.get("link")
        if not link:
            continue
        title = entry.get("title") or ""
        summary = entry.get("summary") or entry.get("description") or ""
        # Final safety: drop entries where no alias appears as a whole word.
        if not _strict_match(f"{title} {summary}", variants):
            continue
        # Google News wraps the source domain inside the description — pull
        # it out so UI shows pb.pl / bankier.pl etc., not "news.google.com".
        src_domain = None
        src_el = entry.get("source")
        if isinstance(src_el, dict):
            src_domain = (src_el.get("title") or "").lower().strip() or None
        if not src_domain:
            src_domain = urlparse(link).netloc.lower().replace("www.", "") or "google-news"
        out.append(
            RawArticle(
                url=str(link),
                title=str(title) or None,
                source=src_domain,
                published_at=_parse_dt(entry.get("published") or entry.get("updated")),
                language="pl",
                summary=(summary[:2000] if summary else None),
            )
        )
    logger.info("GoogleNews: %d articles for %r", len(out), query[:120])
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
            # GDELT intermittently returns HTML error pages with a 200 status
            # (rate-limit / search-too-broad). Guard json() on content-type.
            ctype = (r.headers.get("content-type") or "").lower()
            body = r.text.lstrip()
            if "json" not in ctype and not body.startswith(("{", "[")):
                logger.info(
                    "GDELT: non-JSON response (%s chars, ct=%r) — skipping",
                    len(body), ctype[:80],
                )
                return []
            data = r.json()
    except Exception as e:
        logger.warning("GDELT error: %s", e)
        return []
    arts = data.get("articles") or data.get("article") or [] if isinstance(data, dict) else []
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
            with httpx.Client(
                timeout=25.0,
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"},
                follow_redirects=True,
            ) as client:
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
            if not _strict_match(haystack, needles):
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
        fetch_google_news(company_name, aliases),
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
