"""Reputation & Investment-risk scoring.

Each article contributes a risk-point payload that decays with time,
is weighted by source authority and lexicon category weight, and is
amplified by negative sentiment. The per-company score is the sum
(capped at 100) plus a mirror investment-risk score computed from
per-article `investment_risk` + `investment_impact`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis.risk_lexicon import RISK_CATEGORIES
from app.analysis.risk_verdict import build_verdict, compute_signals
from app.models import Article, ArticleAnalysis, Company, ScoreHistory
from app.scoring.event_lifecycle import calculate_company_score

SOURCE_AUTHORITY: dict[str, float] = {
    "pb.pl": 1.0,
    "bankier.pl": 0.95,
    "money.pl": 0.85,
    "wyborcza.biz": 0.9,
    "rp.pl": 0.95,
    "forsal.pl": 0.95,
    "businessinsider.com.pl": 0.85,
    "reuters.com": 1.0,
    "bloomberg.com": 1.0,
    "ft.com": 1.0,
    "wsj.com": 1.0,
    "gdelt": 0.55,
    "unknown": 0.4,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "decay_per_day": 0.025,
    "max_score": 100.0,
    "reputational_divisor": 2.2,
    "investment_divisor": 2.2,
    "source_authority": SOURCE_AUTHORITY,
}


RECOMMENDATIONS = [
    (85, "Avoid", "Nie rekomendujemy współpracy bez dalszej analizy"),
    (65, "Caution", "Współpraca wyłącznie z zaawansowanym due diligence"),
    (35, "Monitor", "Możliwa współpraca, zalecany bieżący monitoring"),
    (0, "Proceed", "Brak istotnych sygnałów ryzyka"),
]


def recommendation_for(score: float) -> tuple[str, str]:
    for threshold, label, description in RECOMMENDATIONS:
        if score >= threshold:
            return label, description
    return "Proceed", "Brak istotnych sygnałów ryzyka"


def _authority_for_source(source: Optional[str]) -> float:
    if not source:
        return SOURCE_AUTHORITY["unknown"]
    s = source.lower().strip()
    if s in SOURCE_AUTHORITY:
        return SOURCE_AUTHORITY[s]
    for k, v in SOURCE_AUTHORITY.items():
        if k in s:
            return v
    return SOURCE_AUTHORITY["unknown"]


def _category_weight(categories: list[str] | None) -> float:
    if not categories:
        return 0.0
    return max(
        (float(RISK_CATEGORIES.get(c, {}).get("weight", 0.0)) for c in categories),
        default=0.0,
    )


@dataclass
class ArticleScore:
    article_id: str
    published_at: datetime | None
    source: str | None
    reputational_risk: float
    investment_risk: float
    risk_level: str | None
    category: str | None
    recency_weight: float
    authority: float


def _scan_articles(db: Session, company_id: str, lookback_days: int) -> list[tuple[Article, ArticleAnalysis]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    effective = func.coalesce(Article.published_at, Article.scraped_at)
    stmt = (
        select(Article, ArticleAnalysis)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .where(Article.company_id == company_id)
        .where(effective >= cutoff)
        .where(ArticleAnalysis.mentions_company.is_(True))
    )
    return list(db.execute(stmt).all())


def score_articles(rows: list[tuple[Article, ArticleAnalysis]], *, config: dict[str, Any] | None = None) -> tuple[float, float, list[ArticleScore], dict[str, Any]]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    decay = float(cfg["decay_per_day"])
    now = datetime.now(timezone.utc)

    rep_total = 0.0
    inv_total = 0.0
    details: list[ArticleScore] = []
    category_hist: dict[str, float] = {}
    recency_accum = 0.0
    authority_accum = 0.0

    credibility_accum = 0.0
    low_cred_count = 0

    for art, an in rows:
        cats = an.risk_categories or ([an.risk_category] if an.risk_category else [])
        cat_weight = _category_weight([c for c in cats if c])
        severity = float(an.severity or 0.0)
        inv_risk = float(an.investment_risk or 0.0)
        sentiment = float(an.sentiment_score or 0.0)
        credibility = float(an.credibility_score) if an.credibility_score is not None else 0.7

        pub = art.published_at or art.scraped_at or now
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        days_old = max(0, (now - pub).days)
        recency = math.exp(-decay * days_old)
        authority = _authority_for_source(art.source)

        # Negative sentiment amplifies risk up to 2x.
        sent_mult = 1.0 + max(0.0, -sentiment) * 0.8
        sent_mult = min(2.0, max(1.0, sent_mult))

        # Credibility weight — fake/dubious articles have dramatically less impact.
        # Below 0.35 they're essentially ignored; above 0.8 they count fully.
        if an.is_likely_fake:
            cred_weight = 0.05
            low_cred_count += 1
        elif credibility < 0.35:
            cred_weight = 0.1
            low_cred_count += 1
        elif credibility < 0.6:
            cred_weight = 0.5
        else:
            cred_weight = credibility  # 0.6 .. 1.0

        # Reputational contribution = severity * category weight * sentiment * recency * authority * cred
        rep_contribution = (severity * 0.7 + cat_weight * 0.5) * sent_mult * recency * authority * cred_weight
        inv_contribution = inv_risk * sent_mult * recency * authority * cred_weight

        if an.investment_impact == "positive":
            inv_contribution *= 0.2
            rep_contribution *= 0.5

        rep_total += rep_contribution
        inv_total += inv_contribution
        recency_accum += recency
        authority_accum += authority
        credibility_accum += credibility

        for c in cats:
            if c:
                category_hist[c] = category_hist.get(c, 0.0) + rep_contribution

        details.append(
            ArticleScore(
                article_id=str(art.id),
                published_at=pub,
                source=art.source,
                reputational_risk=round(rep_contribution, 3),
                investment_risk=round(inv_contribution, 3),
                risk_level=an.risk_level,
                category=cats[0] if cats else None,
                recency_weight=round(recency, 3),
                authority=authority,
            )
        )

    rep_divisor = float(cfg["reputational_divisor"]) or 1.0
    inv_divisor = float(cfg["investment_divisor"]) or 1.0
    cap = float(cfg["max_score"])

    rep_score = min(cap, rep_total / rep_divisor)
    inv_score = min(cap, inv_total / inv_divisor)

    # Category breakdown in %
    total_cat = sum(category_hist.values()) or 1.0
    category_breakdown = {k: round(100 * v / total_cat, 1) for k, v in category_hist.items()}

    components = {
        "reputational_raw": round(rep_total, 2),
        "investment_raw": round(inv_total, 2),
        "avg_recency": round(recency_accum / max(1, len(rows)), 3),
        "avg_authority": round(authority_accum / max(1, len(rows)), 3),
        "avg_credibility": round(credibility_accum / max(1, len(rows)), 3),
        "low_credibility_count": low_cred_count,
        "article_count": len(rows),
        "category_breakdown": category_breakdown,
    }
    return rep_score, inv_score, details, components


def recalculate_and_persist(
    db: Session, company_id: str, *, lookback_days: int = 90, config: dict[str, Any] | None = None
) -> ScoreHistory:
    """Build a single unified verdict (AI + rules) and persist it.

    The legacy per-article metrics are still computed but used only as a
    transparent breakdown in score_components["legacy"].  The authoritative
    number that drives UI, recommendation and score history is the verdict.
    """
    company = db.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company not found: {company_id}")

    now = datetime.now(timezone.utc)

    # Build the single source of truth
    verdict = build_verdict(db, company, as_of=now)
    signals = compute_signals(db, company_id, as_of=now, lookback_days=lookback_days)

    # Legacy breakdown — mentions_company only, kept for auditing and category histogram
    rows = _scan_articles(db, company_id, lookback_days)
    rep_score_legacy, inv_score_legacy, details, legacy_components = score_articles(rows, config=config)

    ledger = calculate_company_score(db, company_id, as_of=now)

    components: dict[str, Any] = dict(legacy_components)
    components["verdict"] = verdict.as_dict()
    components["signals"] = signals.as_dict()
    components["overall_score"] = verdict.risk_score
    components["recommendation"] = verdict.recommendation
    components["recommendation_description"] = verdict.recommendation_description
    components["confidence"] = verdict.confidence
    components["status"] = verdict.status
    components["rationale"] = verdict.rationale
    components["key_concerns"] = verdict.key_concerns
    components["key_positives"] = verdict.key_positives
    components["overrides"] = verdict.overrides
    components["legacy"] = {
        "article_reputational_score": round(rep_score_legacy, 2),
        "article_investment_score": round(inv_score_legacy, 2),
    }
    components["ledger"] = ledger
    components["event_contributions"] = ledger["breakdown"]
    components["top_articles"] = sorted(
        [d.__dict__ | {"published_at": d.published_at.isoformat() if d.published_at else None} for d in details],
        key=lambda x: x["reputational_risk"],
        reverse=True,
    )[:5]

    # ALWAYS persist a snapshot — even if insufficient_evidence, so the company
    # becomes visible in the ledger with a clear status.
    snap = ScoreHistory(
        company_id=company_id,
        score=float(verdict.risk_score),
        investment_score=float(verdict.risk_score),  # unified single score
        recommendation=verdict.recommendation,
        score_components=components,
        article_count=int(signals.total_articles),
        ledger_score=float(ledger["score"]),
        active_event_count=int(ledger["active_events"]),
        sanctions_match_count=int(ledger["sanctions_hits"]),
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def latest_score_for_company(db: Session, company_id: str) -> ScoreHistory | None:
    stmt = (
        select(ScoreHistory)
        .where(ScoreHistory.company_id == company_id)
        .order_by(ScoreHistory.timestamp.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def score_history_series(db: Session, company_id: str, days: int = 90) -> list[ScoreHistory]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(ScoreHistory)
        .where(ScoreHistory.company_id == company_id)
        .where(ScoreHistory.timestamp >= cutoff)
        .order_by(ScoreHistory.timestamp.asc())
    )
    return list(db.execute(stmt).scalars().all())


def ensure_initial_snapshot(db: Session, company_id: str) -> ScoreHistory | None:
    latest = latest_score_for_company(db, company_id)
    if latest:
        return latest
    return recalculate_and_persist(db, company_id, lookback_days=90)


def top_risk_companies(db: Session, limit: int = 10) -> list[dict[str, Any]]:
    companies = list(db.scalars(select(Company)).all())
    rows: list[dict[str, Any]] = []
    for c in companies:
        latest = latest_score_for_company(db, c.id)
        if latest is None:
            latest = recalculate_and_persist(db, c.id, lookback_days=90)
        article_count = int(
            db.scalar(select(func.count()).select_from(Article).where(Article.company_id == c.id)) or 0
        )
        rows.append(
            {
                "id": c.id,
                "name": c.name,
                "nip": c.nip,
                "ticker": c.ticker,
                "sector": c.sector,
                "score": float(latest.score),
                "investment_score": float(latest.investment_score or 0.0),
                "recommendation": latest.recommendation,
                "article_count": article_count,
                "timestamp": latest.timestamp.isoformat() if latest.timestamp else None,
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:limit]
