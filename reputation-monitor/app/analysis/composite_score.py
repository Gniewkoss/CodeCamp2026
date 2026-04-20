"""Composite risk score — combine 5 pillar sub-scores into a unified 0..100.

Pillars (from config.Settings):
    Financial   — balance sheet health + Claude balance verdict + trend
    Commercial  — contract intensity + payment reputation + trade credit limit
    Legal       — regulatory events + sanctions + VAT status
    Governance  — person risk flags
    Media       — existing news/sentiment layer (heuristic + signals)

Each pillar is a 0..100 where higher = riskier. The composite is a weighted
average plus R1/R2/R3 hard-overrides that still apply (sanctions, active
bankruptcy, etc. still force minimum floors even when media is clean).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.balance_ai_analyzer import BalanceVerdict
from app.analysis.contract_intensity import ContractIntensity
from app.analysis.financial_metrics import HealthBreakdown
from app.analysis.governance_risk import GovernanceResult
from app.analysis.payment_reputation import PaymentReputationResult
from app.analysis.risk_verdict import FinalVerdict
from app.config import get_settings
from app.models import RegulatoryEvent, RiskEvent


RECOMMENDATION_BANDS = [
    (75.0, "Avoid", "Odradzamy współpracę bez bardzo szczegółowego due diligence."),
    (55.0, "Caution", "Dopuszczalne wyłącznie z dodatkowym DD i monitoringiem sygnałów."),
    (30.0, "Monitor", "Możliwa współpraca przy bieżącym monitoringu finansów, mediów i rejestrów."),
    (0.0, "Proceed", "Brak istotnych czerwonych flag w dostępnych danych."),
]


def _recommendation_for(score: float) -> tuple[str, str]:
    for thr, label, desc in RECOMMENDATION_BANDS:
        if score >= thr:
            return label, desc
    return "Proceed", RECOMMENDATION_BANDS[-1][2]


@dataclass
class PillarScore:
    name: str
    score: float                         # 0..100
    weight: float
    reasons: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    has_data: bool = False               # whether we had any real signal

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class CompositeResult:
    composite_score: float = 0.0
    recommendation: str = "Unknown"
    recommendation_description: str = ""
    pillars: dict[str, PillarScore] = field(default_factory=dict)
    overrides: list[str] = field(default_factory=list)
    key_concerns: list[str] = field(default_factory=list)
    key_positives: list[str] = field(default_factory=list)
    # Sub-score shortcuts for convenience / ScoreHistory columns.
    financial_score: Optional[float] = None
    commercial_score: Optional[float] = None
    legal_score: Optional[float] = None
    governance_score: Optional[float] = None
    media_score: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "composite_score": self.composite_score,
            "recommendation": self.recommendation,
            "recommendation_description": self.recommendation_description,
            "pillars": {k: v.as_dict() for k, v in self.pillars.items()},
            "overrides": self.overrides,
            "key_concerns": self.key_concerns,
            "key_positives": self.key_positives,
            "financial_score": self.financial_score,
            "commercial_score": self.commercial_score,
            "legal_score": self.legal_score,
            "governance_score": self.governance_score,
            "media_score": self.media_score,
        }


# ────────────────────────────────────────────────────────────────────────
# Pillar builders
# ────────────────────────────────────────────────────────────────────────


def _financial_pillar(
    health: Optional[HealthBreakdown],
    balance: Optional[BalanceVerdict],
    weight: float,
) -> PillarScore:
    p = PillarScore(name="financial", score=50.0, weight=weight)
    if health is None and balance is None:
        p.reasons.append("Brak danych finansowych.")
        return p

    base = health.score if health else 50.0

    # Balance verdict condition moves score up/down by up to ±12.
    if balance is not None:
        cond = balance.condition
        if cond == "excellent":
            base -= 10.0
        elif cond == "good":
            base -= 5.0
        elif cond == "watch":
            base += 7.0
        elif cond == "distress":
            base += 12.0
        # Solvency forecast overlays:
        if balance.solvency_forecast_12m == "high":
            base = max(base, 75.0)
        elif balance.solvency_forecast_12m == "medium":
            base = max(base, 45.0)

    p.score = max(0.0, min(100.0, base))
    p.has_data = True

    if health:
        p.reasons.extend(health.reasons)
    if balance:
        if balance.red_flags:
            p.reasons.extend(balance.red_flags[:3])
        if balance.strengths:
            p.positives.extend(balance.strengths[:3])
    return p


def _commercial_pillar(
    contract: Optional[ContractIntensity],
    payment: Optional[PaymentReputationResult],
    weight: float,
) -> PillarScore:
    p = PillarScore(name="commercial", score=50.0, weight=weight)
    parts: list[float] = []
    if contract is not None:
        parts.append(contract.score)
        p.reasons.extend(contract.red_flags)
        p.positives.extend(contract.positives)
        p.has_data = p.has_data or contract.last_12m_count > 0 or contract.prior_12m_count > 0
    if payment is not None:
        parts.append(payment.score)
        if payment.events_count:
            p.reasons.append(f"{payment.events_count} wzmianek o problemach z płatnościami.")
        if payment.dbt_flag == "severely_late":
            p.reasons.append("Sygnał bardzo opóźnionych płatności.")
        elif payment.dbt_flag == "on_time":
            p.positives.append("Brak sygnałów o opóźnieniach w płatnościach.")
        p.has_data = p.has_data or payment.dbt_flag != "unknown"
    if parts:
        p.score = sum(parts) / len(parts)
    return p


def _legal_pillar(
    db: Session,
    company_id: str,
    media_signals_sanctions_active: bool,
    weight: float,
) -> PillarScore:
    p = PillarScore(name="legal", score=25.0, weight=weight)
    events = list(
        db.scalars(
            select(RegulatoryEvent)
            .where(RegulatoryEvent.company_id == company_id)
            .order_by(RegulatoryEvent.detected_at.desc())
        ).all()
    )
    active_events = [e for e in events if (e.status or "active") == "active"]
    risk_events = list(
        db.scalars(
            select(RiskEvent).where(
                RiskEvent.company_id == company_id,
                RiskEvent.is_excluded.is_(False),
                RiskEvent.status == "active",
            )
        ).all()
    )

    score = 25.0
    critical_kinds = {"bankruptcy", "liquidation", "disqualification"}
    if any(e.kind in critical_kinds for e in active_events):
        score = max(score, 95.0)
        p.reasons.append("Aktywne zdarzenie krytyczne w rejestrze KRS (upadłość/likwidacja).")
    elif any(e.kind == "restructuring" for e in active_events):
        score = max(score, 70.0)
        p.reasons.append("Aktywne postępowanie restrukturyzacyjne.")
    elif active_events:
        score = max(score, 55.0)
        p.reasons.append(f"{len(active_events)} aktywnych wpisów w KRS dział 6.")

    # Sankcje / risk events from the legacy risk layer count here too.
    if media_signals_sanctions_active:
        score = max(score, 90.0)
        p.reasons.append("Wpis na liście sankcyjnej.")

    if risk_events:
        score = max(score, 40.0 + min(30.0, len(risk_events) * 5.0))
        p.reasons.append(f"{len(risk_events)} aktywnych zdarzeń ryzyka w ledgerze.")

    if not active_events and not risk_events and not media_signals_sanctions_active:
        p.positives.append("Brak aktywnych zdarzeń w rejestrach ani sankcjach.")

    p.score = min(100.0, score)
    p.has_data = True  # legal pillar is always "observed" (absence is a signal)
    return p


def _governance_pillar(gov: Optional[GovernanceResult], weight: float) -> PillarScore:
    p = PillarScore(name="governance", score=35.0, weight=weight)
    if gov is None:
        p.reasons.append("Nie sprawdzono osób z KRS — brak danych governance.")
        return p
    p.score = gov.score
    p.has_data = gov.people_checked > 0
    if gov.flags_count:
        p.reasons.append(f"{gov.flags_count} flag ryzyka na osobach zarządu.")
    else:
        p.positives.append(f"Brak flag ryzyka na {gov.people_checked} osobach zarządu.")
    return p


def _media_pillar(verdict: Optional[FinalVerdict], weight: float) -> PillarScore:
    p = PillarScore(name="media", score=30.0, weight=weight)
    if verdict is None:
        p.reasons.append("Brak danych medialnych.")
        return p
    p.score = float(verdict.risk_score)
    p.reasons.extend(verdict.key_concerns[:3])
    p.positives.extend(verdict.key_positives[:3])
    p.has_data = verdict.status != "insufficient_evidence"
    return p


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────


def build_composite(
    db: Session,
    company_id: str,
    *,
    media_verdict: Optional[FinalVerdict] = None,
    financial_health: Optional[HealthBreakdown] = None,
    balance_verdict: Optional[BalanceVerdict] = None,
    contract_intensity: Optional[ContractIntensity] = None,
    payment_reputation: Optional[PaymentReputationResult] = None,
    governance: Optional[GovernanceResult] = None,
    media_signals_sanctions_active: bool = False,
) -> CompositeResult:
    settings = get_settings()
    weights = {
        "financial": settings.score_weight_financial,
        "commercial": settings.score_weight_commercial,
        "legal": settings.score_weight_legal,
        "governance": settings.score_weight_governance,
        "media": settings.score_weight_media,
    }

    pillars: dict[str, PillarScore] = {
        "financial": _financial_pillar(financial_health, balance_verdict, weights["financial"]),
        "commercial": _commercial_pillar(contract_intensity, payment_reputation, weights["commercial"]),
        "legal": _legal_pillar(db, company_id, media_signals_sanctions_active, weights["legal"]),
        "governance": _governance_pillar(governance, weights["governance"]),
        "media": _media_pillar(media_verdict, weights["media"]),
    }

    # Weighted average. Pillars WITHOUT data get half-weight so missing data
    # doesn't dominate. If nobody has data, fall back to media verdict.
    numerator = 0.0
    denominator = 0.0
    for p in pillars.values():
        w = p.weight * (1.0 if p.has_data else 0.5)
        numerator += p.score * w
        denominator += w
    if denominator <= 0:
        composite = pillars["media"].score if pillars["media"] else 50.0
    else:
        composite = numerator / denominator

    overrides: list[str] = []
    # HARD OVERRIDES — these mirror risk_verdict R1 but at the composite level.
    # They apply regardless of pillar weighting.
    if media_signals_sanctions_active:
        if composite < 80:
            overrides.append(f"R1/Legal: sankcje aktywne → podniesiono {composite:.0f}→80")
            composite = 80.0
    # Critical regulatory event (bankruptcy/liquidation/disqualification)
    has_critical = any(
        r for r in pillars["legal"].reasons if any(k in r.lower() for k in ("upadłość", "likwidacj", "krytyczne"))
    )
    if has_critical and composite < 80:
        overrides.append(f"R1/Legal: aktywne zdarzenie krytyczne → podniesiono {composite:.0f}→80")
        composite = 80.0

    if balance_verdict and balance_verdict.solvency_forecast_12m == "high" and composite < 65:
        overrides.append(f"R-Fin: prognoza niewypłacalności 12m high → podniesiono {composite:.0f}→65")
        composite = 65.0

    composite = max(0.0, min(100.0, round(composite, 1)))
    rec, desc = _recommendation_for(composite)

    # Aggregate top 5 concerns / positives across pillars.
    concerns: list[str] = []
    positives: list[str] = []
    for p in pillars.values():
        for r in p.reasons:
            if r and r not in concerns:
                concerns.append(r)
        for r in p.positives:
            if r and r not in positives:
                positives.append(r)

    return CompositeResult(
        composite_score=composite,
        recommendation=rec,
        recommendation_description=desc,
        pillars=pillars,
        overrides=overrides,
        key_concerns=concerns[:6],
        key_positives=positives[:6],
        financial_score=round(pillars["financial"].score, 1),
        commercial_score=round(pillars["commercial"].score, 1),
        legal_score=round(pillars["legal"].score, 1),
        governance_score=round(pillars["governance"].score, 1),
        media_score=round(pillars["media"].score, 1),
    )
