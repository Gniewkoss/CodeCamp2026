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

from app.analysis.balance_ai_analyzer import BalanceVerdict
from app.analysis.composite_score import build_composite
from app.analysis.contract_intensity import ContractIntensity, compute_contract_intensity
from app.analysis.financial_metrics import HealthBreakdown, RatiosPack, compute_ratios, compute_trend, financial_health_score
from app.analysis.financial_extractor import ExtractedFigures
from app.analysis.governance_risk import GovernanceResult
from app.analysis.payment_reputation import PaymentReputationResult, assess_payment_reputation
from app.analysis.risk_lexicon import RISK_CATEGORIES
from app.analysis.risk_verdict import build_verdict, compute_signals
from app.models import (
    Article,
    ArticleAnalysis,
    Company,
    Contract,
    FinancialAIAnalysis,
    FinancialFigures,
    FinancialStatement,
    PaymentReputation as PaymentReputationRow,
    ScoreHistory,
)
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


def _load_financial_inputs(
    db: Session, company_id: str
) -> tuple[Optional[HealthBreakdown], Optional[BalanceVerdict], list[ExtractedFigures], list[RatiosPack]]:
    """Load financial health + balance AI verdict for the composite scorer.

    Pulls up to 3 most recent FinancialStatement rows with their figures,
    re-computes ratios/trend and reads the latest FinancialAIAnalysis (if any).
    """
    stmt = (
        select(FinancialStatement, FinancialFigures)
        .join(FinancialFigures, FinancialFigures.statement_id == FinancialStatement.id)
        .where(FinancialStatement.company_id == company_id)
        .order_by(FinancialStatement.period_end.desc())
        .limit(3)
    )
    rows = db.execute(stmt).all()
    figures_by_year: list[ExtractedFigures] = []
    for s, f in rows:
        ex = ExtractedFigures(
            period_end=s.period_end or "",
            period_type=s.period_type or "annual",
            currency=s.currency or "PLN",
            source=s.source or "UNKNOWN",
            revenue=f.revenue,
            cost_of_revenue=f.cost_of_revenue,
            operating_costs=f.operating_costs,
            ebit=f.ebit,
            ebitda=f.ebitda,
            net_profit=f.net_profit,
            total_assets=f.total_assets,
            current_assets=f.current_assets,
            non_current_assets=f.non_current_assets,
            cash=f.cash,
            inventory=f.inventory,
            receivables=f.receivables,
            total_liabilities=f.total_liabilities,
            current_liabilities=f.current_liabilities,
            non_current_liabilities=f.non_current_liabilities,
            trade_payables=f.trade_payables,
            equity=f.equity,
            retained_earnings=f.retained_earnings,
            cash_from_operations=f.cash_from_operations,
            capex=f.capex,
            insurance_costs_mentioned=f.insurance_costs_mentioned,
        )
        figures_by_year.append(ex)
    ratios_by_year = [compute_ratios(f) for f in figures_by_year]
    trend = compute_trend(figures_by_year) if figures_by_year else None
    health = financial_health_score(ratios_by_year, trend=trend) if ratios_by_year else None

    ai_row = db.scalar(
        select(FinancialAIAnalysis)
        .where(FinancialAIAnalysis.company_id == company_id)
        .order_by(FinancialAIAnalysis.as_of.desc())
        .limit(1)
    )
    balance_verdict: Optional[BalanceVerdict] = None
    if ai_row is not None:
        balance_verdict = BalanceVerdict(
            condition=ai_row.condition or "unknown",
            red_flags=list(ai_row.red_flags or []),
            strengths=list(ai_row.strengths or []),
            short_term_risks=list(ai_row.short_term_risks or []),
            long_term_risks=list(ai_row.long_term_risks or []),
            commentary=ai_row.commentary or "",
            solvency_forecast_12m=ai_row.solvency_forecast_12m or "medium",
            years_covered=list(ai_row.years_covered or []),
        )
    return health, balance_verdict, figures_by_year, ratios_by_year


def _load_contract_intensity(db: Session, company_id: str) -> Optional[ContractIntensity]:
    contracts = list(
        db.scalars(
            select(Contract)
            .where(Contract.company_id == company_id)
            .order_by(Contract.detected_at.desc())
            .limit(300)
        ).all()
    )
    if not contracts:
        return None
    return compute_contract_intensity(contracts)


def _load_payment_reputation(db: Session, company_id: str, *, nip: Optional[str]) -> Optional[PaymentReputationResult]:
    # Prefer the latest persisted PaymentReputation row if fresh; otherwise run now.
    latest = db.scalar(
        select(PaymentReputationRow)
        .where(PaymentReputationRow.company_id == company_id)
        .order_by(PaymentReputationRow.as_of.desc())
        .limit(1)
    )
    if latest is not None:
        return PaymentReputationResult(
            score=float(latest.score or 50.0),
            dbt_flag=latest.dbt_flag or "unknown",
            dpo_days=latest.dpo_days,
            events_count=int(latest.events_count or 0),
            news_mentions=list(latest.news_mentions or []),
            sources=dict(latest.sources or {}),
        )
    # Only compute on-demand when at least one article + one statement exist,
    # so we don't hammer the DB on cold scans.
    return None


def recalculate_and_persist(
    db: Session, company_id: str, *, lookback_days: int = 90, config: dict[str, Any] | None = None
) -> ScoreHistory:
    """Build a unified verdict (composite score across 5 pillars) and persist it.

    The media verdict (`build_verdict`) is still computed and used as the
    ``media`` pillar + recommendation-band reference. The authoritative number
    exposed to the UI is the composite score.
    """
    company = db.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company not found: {company_id}")

    now = datetime.now(timezone.utc)

    media_verdict = build_verdict(db, company, as_of=now)
    signals = compute_signals(db, company_id, as_of=now, lookback_days=lookback_days)

    rows = _scan_articles(db, company_id, lookback_days)
    rep_score_legacy, inv_score_legacy, details, legacy_components = score_articles(rows, config=config)

    ledger = calculate_company_score(db, company_id, as_of=now)

    # ── New pillar inputs ──────────────────────────────────────────
    health, balance_verdict, figures_by_year, _ratios = _load_financial_inputs(db, company_id)
    contract_intensity = _load_contract_intensity(db, company_id)
    payment_reputation = _load_payment_reputation(db, company_id, nip=company.nip)
    # Governance is computed during the scrape (it's expensive) and persisted
    # via PersonRiskFlag — we re-derive an aggregate from flags here.
    governance = _aggregate_governance(db, company_id)

    composite = build_composite(
        db,
        company_id,
        media_verdict=media_verdict,
        financial_health=health,
        balance_verdict=balance_verdict,
        contract_intensity=contract_intensity,
        payment_reputation=payment_reputation,
        governance=governance,
        media_signals_sanctions_active=signals.sanctions_active,
    )

    components: dict[str, Any] = dict(legacy_components)
    components["verdict"] = media_verdict.as_dict()
    components["signals"] = signals.as_dict()
    components["composite"] = composite.as_dict()
    components["overall_score"] = composite.composite_score
    components["recommendation"] = composite.recommendation
    components["recommendation_description"] = composite.recommendation_description
    components["confidence"] = media_verdict.confidence
    components["status"] = media_verdict.status
    components["rationale"] = media_verdict.rationale
    components["key_concerns"] = composite.key_concerns or media_verdict.key_concerns
    components["key_positives"] = composite.key_positives or media_verdict.key_positives
    components["overrides"] = (media_verdict.overrides or []) + (composite.overrides or [])
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

    snap = ScoreHistory(
        company_id=company_id,
        score=float(composite.composite_score),
        investment_score=float(composite.composite_score),
        recommendation=composite.recommendation,
        score_components=components,
        article_count=int(signals.total_articles),
        ledger_score=float(ledger["score"]),
        active_event_count=int(ledger["active_events"]),
        sanctions_match_count=int(ledger["sanctions_hits"]),
        composite_score=float(composite.composite_score),
        financial_score=composite.financial_score,
        commercial_score=composite.commercial_score,
        legal_score=composite.legal_score,
        governance_score=composite.governance_score,
        media_score=composite.media_score,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def _aggregate_governance(db: Session, company_id: str) -> Optional[GovernanceResult]:
    """Rebuild a GovernanceResult from PersonRiskFlag rows persisted during scraping."""
    from app.models import CompanyPerson, PersonRiskFlag

    people = list(
        db.scalars(
            select(CompanyPerson).where(
                CompanyPerson.company_id == company_id,
                CompanyPerson.is_active.is_(True),
            )
        ).all()
    )
    if not people:
        return None
    person_ids = [p.id for p in people]
    flags = list(
        db.scalars(
            select(PersonRiskFlag).where(PersonRiskFlag.person_id.in_(person_ids))
        ).all()
    )
    flagged: dict[str, list[dict[str, Any]]] = {}
    total_severity = 0.0
    for f in flags:
        total_severity += float(f.severity or 0.0)
        flagged.setdefault(f.person_id, []).append(
            {
                "kind": f.kind,
                "other_company_name": f.other_company_name,
                "severity": float(f.severity or 0.0),
                "notes": f.notes,
            }
        )
    persons_out: list[dict[str, Any]] = []
    for p in people:
        bits = flagged.get(p.id)
        if bits:
            persons_out.append({"name": p.full_name, "role": p.role, "flags": bits})
    score = 25.0 + min(75.0, total_severity * 30.0)
    return GovernanceResult(
        score=round(score, 1),
        flags_count=len(flags),
        people_checked=len(people),
        flagged_people=persons_out,
        notes=f"Sprawdzono {len(people)} osób, {len(flags)} flag.",
    )


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
