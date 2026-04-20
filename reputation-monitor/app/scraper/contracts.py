"""Public contracts scraper: TED (EU), BZP / e-Zamówienia (PL), news-scan.

All three sources are best-effort and feature-flagged — failures just return
empty lists so the pipeline keeps going. Aggregated results feed the
``contract_intensity`` module and the Contract SQL model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Article, ArticleAnalysis, Contract

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass
class RawContract:
    source: str                          # TED | BZP | GPW_ESPI | NEWS
    external_ref: Optional[str] = None
    counterparty: Optional[str] = None   # awarding entity
    title: Optional[str] = None
    value_pln: Optional[float] = None
    currency: Optional[str] = "PLN"
    award_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[str] = None
    url: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["award_date"] = self.award_date.isoformat() if self.award_date else None
        d["end_date"] = self.end_date.isoformat() if self.end_date else None
        return d


# ────────────────────────────────────────────────────────────────────────
# TED — Tenders Electronic Daily
# ────────────────────────────────────────────────────────────────────────


TED_API = "https://api.ted.europa.eu/v3/notices/search"


def fetch_ted(name: str, *, nip: Optional[str] = None, months_back: int = 24, limit: int = 30) -> list[RawContract]:
    settings = get_settings()
    if not getattr(settings, "enable_ted", True):
        return []
    # TED's notices search supports a free-text query via ``query`` (ExpertSearch).
    terms: list[str] = []
    if name:
        # Escape quotes for the query.
        terms.append(f'"{name}"')
    if nip:
        terms.append(f'"{nip}"')
    if not terms:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * months_back)).strftime("%Y%m%d")
    payload = {
        "query": f"(contracting-party-name=({' OR '.join(terms)}) OR winner-name=({' OR '.join(terms)})) AND publication-date>={cutoff}",
        "pageNum": 1,
        "pageSize": min(limit, 100),
        "fields": [
            "publication-number",
            "publication-date",
            "title",
            "contract-valuation",
            "winner-name",
            "buyer-name",
            "place-of-performance",
            "links",
        ],
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(TED_API, json=payload)
            if resp.status_code >= 400:
                logger.info("TED %s: %s", resp.status_code, resp.text[:200])
                return []
            data = resp.json()
    except Exception as e:
        logger.info("TED fetch failed: %s", e)
        return []

    out: list[RawContract] = []
    for item in (data.get("notices") or [])[:limit]:
        pub = _parse_iso_date(item.get("publication-date"))
        val = None
        vraw = item.get("contract-valuation") or item.get("estimated-value")
        if isinstance(vraw, dict):
            amount = vraw.get("amount") or vraw.get("value")
            try:
                val = float(amount) if amount is not None else None
            except Exception:
                val = None
        title = _first_lang(item.get("title")) or ""
        buyer = _first_lang(item.get("buyer-name")) or _first_lang(item.get("contracting-party-name"))
        links = item.get("links") or {}
        pdf_url = None
        if isinstance(links, dict):
            pdf_url = (links.get("pdf") or {}).get("href") if isinstance(links.get("pdf"), dict) else links.get("pdf")
        out.append(
            RawContract(
                source="TED",
                external_ref=str(item.get("publication-number") or "")[:128],
                counterparty=buyer,
                title=title[:400] if title else None,
                value_pln=val,  # TED's native currency is EUR; treated as raw value
                currency="EUR",
                award_date=pub,
                status="active",
                url=pdf_url,
                raw=item,
            )
        )
    return out


def _first_lang(obj: Any) -> Optional[str]:
    """TED returns multilingual dicts like ``{"pol": "...", "eng": "..."}``."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("pol", "eng", "fra", "deu"):
            if obj.get(key):
                return str(obj[key])
        for v in obj.values():
            if v:
                return str(v)
    if isinstance(obj, list) and obj:
        return _first_lang(obj[0])
    return None


def _parse_iso_date(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        s = str(raw)
        # TED uses YYYY-MM-DD+HH:MM for some fields.
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:25])
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────
# BZP — Biuletyn Zamówień Publicznych / e-Zamówienia
# ────────────────────────────────────────────────────────────────────────


BZP_API = "https://ezamowienia.gov.pl/mo-client-board/bzp/public/notices/list"


def fetch_bzp(name: str, *, nip: Optional[str] = None, limit: int = 30) -> list[RawContract]:
    settings = get_settings()
    if not getattr(settings, "enable_bzp", True):
        return []
    q = nip or name
    if not q:
        return []
    params = {
        "Size": min(limit, 50),
        "OrganizationTaxId": nip or "",
        "Query": name or "",
        "Page": 1,
    }
    try:
        with httpx.Client(timeout=10.0, headers={"Accept": "application/json"}) as client:
            resp = client.get(BZP_API, params=params)
            if resp.status_code >= 400:
                logger.info("BZP %s: %s", resp.status_code, resp.text[:200])
                return []
            data = resp.json()
    except Exception as e:
        logger.info("BZP fetch failed: %s", e)
        return []

    items = data.get("content") or data.get("items") or data.get("data") or []
    out: list[RawContract] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        val = _coerce_float(item.get("estimatedValue") or item.get("orderValue"))
        out.append(
            RawContract(
                source="BZP",
                external_ref=str(item.get("noticeNumber") or item.get("id") or "")[:128],
                counterparty=str(item.get("organizationName") or item.get("buyerName") or "")[:512] or None,
                title=str(item.get("subject") or item.get("title") or "")[:400] or None,
                value_pln=val,
                currency="PLN",
                award_date=_parse_iso_date(item.get("publicationDate") or item.get("contractDate")),
                status="active",
                url=item.get("url") or None,
                raw=item,
            )
        )
    return out


def _coerce_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        s = str(x).replace(" ", "").replace("\xa0", "")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return float(s)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────
# News-scan — reuse already-persisted articles
# ────────────────────────────────────────────────────────────────────────


_CONTRACT_KEYWORDS = [
    "kontrakt",
    "umow",
    "zamówienie",
    "zamowienie",
    "wart",
    "mln zł",
    "mld zł",
    "podpisa",
]

_VALUE_RE = re.compile(
    r"(\d+(?:[\s\xa0]\d{3})*(?:[.,]\d+)?)\s*(mln|mld|tys)?\s*(?:zł|PLN)",
    re.IGNORECASE,
)


def scan_contracts_in_news(db: Session, company_id: str, *, limit: int = 20) -> list[RawContract]:
    stmt = (
        select(Article, ArticleAnalysis)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .where(Article.company_id == company_id)
        .where(ArticleAnalysis.mentions_company.is_(True))
        .order_by(Article.scraped_at.desc())
        .limit(120)
    )
    rows = db.execute(stmt).all()
    out: list[RawContract] = []
    for art, an in rows:
        blob = " ".join(filter(None, [art.title or "", an.summary or ""])).lower()
        if not blob:
            continue
        if not any(kw in blob for kw in _CONTRACT_KEYWORDS):
            continue
        val = _extract_pln_value(blob)
        out.append(
            RawContract(
                source="NEWS",
                external_ref=str(art.id),
                counterparty=None,
                title=(art.title or an.summary or "")[:400] or None,
                value_pln=val,
                currency="PLN",
                award_date=art.published_at,
                status="reported",
                url=art.url,
                raw={"article_id": art.id, "source_name": art.source},
            )
        )
        if len(out) >= limit:
            break
    return out


def _extract_pln_value(text: str) -> Optional[float]:
    m = _VALUE_RE.search(text)
    if not m:
        return None
    raw_num = m.group(1).replace("\xa0", "").replace(" ", "")
    raw_num = raw_num.replace(".", "").replace(",", ".") if ("," in raw_num and "." in raw_num) else raw_num.replace(",", ".")
    try:
        num = float(raw_num)
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit == "mld":
        num *= 1_000_000_000
    elif unit == "mln":
        num *= 1_000_000
    elif unit == "tys":
        num *= 1_000
    return num


# ────────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────────


def collect_contracts(
    db: Session,
    company_id: str,
    *,
    name: str,
    nip: Optional[str] = None,
    months_back: int = 24,
) -> list[Contract]:
    """Fetch contracts from all enabled sources and persist new ones.

    Returns the list of persisted (possibly pre-existing) Contract rows for
    this company, limited to the configured lookback.
    """
    raws: list[RawContract] = []
    try:
        raws.extend(fetch_ted(name, nip=nip, months_back=months_back))
    except Exception as e:
        logger.info("TED skipped: %s", e)
    try:
        raws.extend(fetch_bzp(name, nip=nip))
    except Exception as e:
        logger.info("BZP skipped: %s", e)
    try:
        raws.extend(scan_contracts_in_news(db, company_id))
    except Exception as e:
        logger.info("News-contract scan skipped: %s", e)

    added = 0
    for raw in raws:
        if raw.external_ref:
            existing = db.scalar(
                select(Contract).where(
                    Contract.company_id == company_id,
                    Contract.source == raw.source,
                    Contract.external_ref == raw.external_ref,
                )
            )
            if existing:
                continue
        row = Contract(
            company_id=company_id,
            source=raw.source,
            external_ref=raw.external_ref,
            counterparty=raw.counterparty,
            title=raw.title,
            value_pln=raw.value_pln,
            currency=raw.currency or "PLN",
            award_date=raw.award_date,
            end_date=raw.end_date,
            status=raw.status,
            url=raw.url,
            raw_payload=raw.raw,
        )
        db.add(row)
        added += 1
    try:
        db.commit()
    except Exception as e:
        logger.warning("Contracts commit failed: %s", e)
        db.rollback()

    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
    stmt = (
        select(Contract)
        .where(Contract.company_id == company_id)
        .where((Contract.award_date >= cutoff) | (Contract.award_date.is_(None)))
        .order_by(Contract.detected_at.desc())
        .limit(200)
    )
    rows = list(db.scalars(stmt).all())
    if added:
        logger.info("Contracts: added %d new rows for %s", added, company_id)
    return rows
