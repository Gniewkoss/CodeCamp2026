from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.analysis.risk_lexicon import RISK_KEYWORDS, categories_from_stored_keywords, keyword_weight_sum_for_categories
from app.database import SessionLocal
from app.models import Article, ArticleAnalysis, Company, ScoreHistory

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

SOURCE_AUTHORITY: dict[str, float] = {
    "pb.pl": 1.0,
    "bankier.pl": 0.9,
    "money.pl": 0.8,
    "wyborcza.biz": 0.85,
    "reuters.com": 1.0,
    "bloomberg.com": 1.0,
    "gdelt": 0.5,
    "unknown": 0.3,
}

DEFAULT_SCORING_CONFIG: dict[str, Any] = {
    "decay_per_day": 0.03,
    "max_score": 100.0,
    "sentiment_scale": 1.0,
    "normalize_divisor": 1.0,
    "source_authority": SOURCE_AUTHORITY,
    "risk_weights": {k: v["weight"] for k, v in RISK_KEYWORDS.items()},
}


def _authority_for_source(source: str | None, config: dict[str, Any]) -> float:
    auth = config.get("source_authority") or SOURCE_AUTHORITY
    if not source:
        return float(auth.get("unknown", 0.3))
    s = source.lower().strip()
    if s in auth:
        return float(auth[s])
    for k, v in auth.items():
        if k in s or s.endswith(k):
            return float(v)
    return float(auth.get("unknown", 0.3))


@dataclass
class ArticleRiskRow:
    article_id: uuid.UUID
    published_at: datetime | None
    source: str | None
    sentiment_score: float
    risk_keywords: list[str] | None
    categories: list[str]


def get_recent_analyses(
    db: "Session", company_id: uuid.UUID, lookback_days: int = 90
) -> list[ArticleRiskRow]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    effective = func.coalesce(Article.published_at, Article.scraped_at)
    stmt = (
        select(Article, ArticleAnalysis)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .where(Article.company_id == company_id)
        .where(effective >= cutoff)
    )
    rows = db.execute(stmt).all()
    out: list[ArticleRiskRow] = []
    for art, an in rows:
        cats = categories_from_stored_keywords(an.risk_keywords)
        if an.raw_llm_response and not cats:
            try:
                import json

                data = json.loads(an.raw_llm_response)
                for c in data.get("risk_categories") or []:
                    cs = str(c).lower()
                    if cs in RISK_KEYWORDS and cs not in cats:
                        cats.append(cs)
            except Exception:
                pass
        out.append(
            ArticleRiskRow(
                article_id=art.id,
                published_at=art.published_at or art.scraped_at,
                source=art.source,
                sentiment_score=float(an.sentiment_score or 0.0),
                risk_keywords=an.risk_keywords,
                categories=cats,
            )
        )
    return out


def calculate_score(
    company_id: uuid.UUID,
    lookback_days: int = 90,
    *,
    db: "Session | None" = None,
    config: dict[str, Any] | None = None,
    persist: bool = True,
) -> float:
    cfg = {**DEFAULT_SCORING_CONFIG, **(config or {})}
    own_session = db is None
    if own_session:
        db = SessionLocal()
    assert db is not None
    try:
        articles = get_recent_analyses(db, company_id, lookback_days)
        now = datetime.now(timezone.utc)
        total_score = 0.0
        comp_keyword = 0.0
        comp_sentiment = 0.0
        comp_recency = 0.0
        comp_authority = 0.0

        for article in articles:
            rw = cfg.get("risk_weights")
            keyword_score = keyword_weight_sum_for_categories(article.categories, rw)
            sentiment_mult = 1 + max(0.0, -article.sentiment_score) * float(cfg.get("sentiment_scale", 1.0))
            sentiment_mult = min(2.0, max(1.0, sentiment_mult))
            pub = article.published_at or now
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            days_old = max(0, (now - pub).days)
            recency_weight = math.exp(-float(cfg.get("decay_per_day", 0.03)) * days_old)
            authority = _authority_for_source(article.source, cfg)
            article_risk = keyword_score * sentiment_mult * recency_weight * authority
            total_score += article_risk
            comp_keyword += keyword_score * recency_weight * authority
            comp_sentiment += (sentiment_mult - 1.0) * keyword_score * recency_weight * authority
            comp_recency += recency_weight
            comp_authority += authority

        cap = float(cfg.get("max_score", 100.0))
        div = float(cfg.get("normalize_divisor", 1.0)) or 1.0
        normalized = min(cap, total_score / div)
        breakdown_raw = {
            "keyword_hits": comp_keyword,
            "sentiment_amplification": comp_sentiment,
            "recency_avg_weight": comp_recency / max(1, len(articles)),
            "authority_avg": comp_authority / max(1, len(articles)),
            "total_raw": total_score,
        }
        s = sum(breakdown_raw[k] for k in ("keyword_hits", "sentiment_amplification") if breakdown_raw[k] > 0)
        score_components = {
            "keyword_hits": round(100 * breakdown_raw["keyword_hits"] / s, 1) if s > 0 else 0.0,
            "sentiment": round(100 * breakdown_raw["sentiment_amplification"] / s, 1) if s > 0 else 0.0,
            "recency": round(100 * breakdown_raw["recency_avg_weight"], 1),
            "source_authority": round(100 * breakdown_raw["authority_avg"], 1),
            "detail": breakdown_raw,
        }

        if persist:
            snap = ScoreHistory(
                company_id=company_id,
                score=float(normalized),
                score_components=score_components,
                article_count=len(articles),
            )
            db.add(snap)
            db.commit()
        return float(normalized)
    finally:
        if own_session and db is not None:
            db.close()


def latest_score_for_company(db: "Session", company_id: uuid.UUID) -> ScoreHistory | None:
    stmt = (
        select(ScoreHistory)
        .where(ScoreHistory.company_id == company_id)
        .order_by(ScoreHistory.timestamp.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def score_history_series(db: "Session", company_id: uuid.UUID, days: int = 90) -> list[ScoreHistory]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(ScoreHistory)
        .where(ScoreHistory.company_id == company_id)
        .where(ScoreHistory.timestamp >= cutoff)
        .order_by(ScoreHistory.timestamp.asc())
    )
    return list(db.execute(stmt).scalars().all())


def top_risk_companies(db: "Session", limit: int = 10) -> list[tuple[Company, float]]:
    """Latest snapshot per company, ranked by score descending.

    Ensures each company has at least one score row when possible (so the UI is not
    stuck on “no snapshot” after seeding without a scan).
    """
    companies = list(db.scalars(select(Company)).all())
    ranked: list[tuple[Company, float]] = []
    for c in companies:
        latest = latest_score_for_company(db, c.id)
        if latest is None:
            calculate_score(c.id, lookback_days=90, db=db, persist=True)
            latest = latest_score_for_company(db, c.id)
        ranked.append((c, float(latest.score) if latest else 0.0))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:limit]
