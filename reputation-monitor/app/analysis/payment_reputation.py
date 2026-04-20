"""Payment-reputation scoring: does the company pay its suppliers on time?

Aggregates:

1. DPO proxy from the latest FinancialFigures
   (trade_payables / operating_costs × 365).
2. News scan for keywords: zaległości, windykacja, egzekucja komornicza,
   pozew o zapłatę, komornik, nakaz zapłaty.
3. Optional BIG InfoMonitor API (feature-flag ``big_infomonitor_api_key``).

Output: ``PaymentReputationResult`` with a 0..100 score (higher = worse),
DBT flag (on_time | late | severely_late | unknown) and the sources it used.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Article, ArticleAnalysis, FinancialFigures, FinancialStatement

logger = logging.getLogger(__name__)


PAYMENT_RED_KEYWORDS = [
    "zaległości",
    "zalegania",
    "windykacj",
    "windykuje",
    "egzekucja komornicza",
    "egzekucj",
    "komornik",
    "pozew o zapłat",
    "nakaz zapłat",
    "nie płaci",
    "nieterminowe płatności",
    "opóźnia płatności",
    "dłużnik",
    "dług",
]

PAYMENT_GREEN_KEYWORDS = [
    "płaci w terminie",
    "terminowe płatności",
    "solidny płatnik",
    "najlepszy pracodawca",
    "rzetelna firma",
]


@dataclass
class PaymentReputationResult:
    score: float = 50.0                 # 0..100, higher = worse
    dbt_flag: str = "unknown"           # on_time | late | severely_late | unknown
    dpo_days: Optional[float] = None
    events_count: int = 0
    news_mentions: list[str] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ────────────────────────────────────────────────────────────────────────
# DPO proxy
# ────────────────────────────────────────────────────────────────────────


def _latest_dpo(db: Session, company_id: str) -> Optional[float]:
    stmt = (
        select(FinancialStatement, FinancialFigures)
        .join(FinancialFigures, FinancialFigures.statement_id == FinancialStatement.id)
        .where(FinancialStatement.company_id == company_id)
        .order_by(FinancialStatement.period_end.desc())
        .limit(1)
    )
    row = db.execute(stmt).first()
    if not row:
        return None
    _, f = row
    if f.trade_payables and f.operating_costs and f.operating_costs > 0:
        return 365.0 * float(f.trade_payables) / float(f.operating_costs)
    if f.trade_payables and f.cost_of_revenue and f.cost_of_revenue > 0:
        return 365.0 * float(f.trade_payables) / float(f.cost_of_revenue)
    return None


# ────────────────────────────────────────────────────────────────────────
# News scan
# ────────────────────────────────────────────────────────────────────────


def _scan_news(db: Session, company_id: str) -> tuple[int, list[str]]:
    stmt = (
        select(Article, ArticleAnalysis)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .where(Article.company_id == company_id)
        .where(ArticleAnalysis.mentions_company.is_(True))
        .order_by(Article.scraped_at.desc())
        .limit(80)
    )
    rows = db.execute(stmt).all()
    hits: list[str] = []
    for art, an in rows:
        blob = " ".join(filter(None, [art.title or "", an.summary or ""])).lower()
        if not blob:
            continue
        for kw in PAYMENT_RED_KEYWORDS:
            if kw in blob:
                title = (art.title or art.url or "")[:160]
                hits.append(f"{kw.strip()}: {title}")
                break
    return len(hits), hits[:5]


# ────────────────────────────────────────────────────────────────────────
# Optional BIG InfoMonitor (feature-flag)
# ────────────────────────────────────────────────────────────────────────


def _query_big(nip: Optional[str]) -> Optional[dict[str, Any]]:
    settings = get_settings()
    key = getattr(settings, "big_infomonitor_api_key", None)
    if not key or not nip:
        return None
    # Placeholder — actual BIG API requires an enterprise contract.
    # We only log that it would be called here so production deployments
    # can plug in their real integration.
    logger.info("BIG InfoMonitor call skipped (placeholder) for NIP %s", nip)
    return None


# ────────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────────


def assess_payment_reputation(
    db: Session,
    company_id: str,
    *,
    nip: Optional[str] = None,
) -> PaymentReputationResult:
    dpo = _latest_dpo(db, company_id)
    news_count, news_list = _scan_news(db, company_id)
    big = _query_big(nip)

    score = 50.0
    dbt = "unknown"
    reasons: dict[str, Any] = {}

    if dpo is not None:
        reasons["dpo_days"] = round(dpo, 1)
        if dpo <= 45:
            score -= 20
            dbt = "on_time"
        elif dpo <= 75:
            score -= 5
            dbt = "on_time"
        elif dpo <= 110:
            score += 15
            dbt = "late"
        else:
            score += 30
            dbt = "severely_late"

    if news_count > 0:
        score += min(30, 6 * news_count)
        # News always downgrades; if DPO was on_time, pull toward late.
        if dbt == "on_time":
            dbt = "late"
        elif dbt == "unknown":
            dbt = "late"
        if news_count >= 3:
            dbt = "severely_late"
        reasons["news_hits"] = news_count

    if big:
        reasons["big"] = big
        # Expected shape: {"dbt_days": N, "overdue_pln": X}.
        dbt_days = big.get("dbt_days")
        if isinstance(dbt_days, (int, float)):
            reasons["dbt_days"] = dbt_days
            if dbt_days <= 10:
                score -= 10
                dbt = "on_time"
            elif dbt_days <= 30:
                score += 5
                dbt = "late"
            else:
                score += 25
                dbt = "severely_late"

    score = _clip(score, 0.0, 100.0)
    return PaymentReputationResult(
        score=round(score, 1),
        dbt_flag=dbt,
        dpo_days=round(dpo, 1) if dpo is not None else None,
        events_count=news_count,
        news_mentions=news_list,
        sources=reasons,
    )
