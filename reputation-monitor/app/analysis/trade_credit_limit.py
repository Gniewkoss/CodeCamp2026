"""Algorithmic trade credit limit (limit kupiecki).

Formula (industry rule-of-thumb, adapted for Polish B2B):

    base = min(0.10 * equity, 0.05 * revenue)
    limit = base * liquidity_factor * altman_factor * insurance_factor * payment_factor * trend_factor

    low  = limit * 0.6
    high = limit * 1.4

where factors are clipped multipliers derived from the latest financial ratios,
the Claude balance verdict, insurance signal, and payment reputation.

The result is a RECOMMENDATION — not an insurer's guaranteed limit — but it
gives the user a concrete number to anchor trade credit decisions on. Every
factor is persisted in ``factors`` so the UI can show a transparent breakdown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.analysis.balance_ai_analyzer import BalanceVerdict
from app.analysis.financial_extractor import ExtractedFigures
from app.analysis.financial_metrics import RatiosPack

logger = logging.getLogger(__name__)


@dataclass
class LimitSuggestion:
    currency: str = "PLN"
    recommended: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    rationale: str = ""
    factors: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _liquidity_factor(current_ratio: Optional[float]) -> float:
    if current_ratio is None:
        return 0.85
    if current_ratio < 0.8:
        return 0.40
    if current_ratio < 1.0:
        return 0.55
    if current_ratio < 1.3:
        return 0.85
    if current_ratio < 2.0:
        return 1.10
    return 1.20


def _altman_factor(z_em: Optional[float]) -> float:
    if z_em is None:
        return 0.85
    if z_em < 1.10:
        return 0.30
    if z_em < 1.80:
        return 0.55
    if z_em < 2.60:
        return 0.85
    return 1.20


def _insurance_factor(state: Optional[str]) -> float:
    if not state:
        return 1.0
    s = state.lower()
    if s == "known_insured":
        return 1.30
    if s == "likely_insured":
        return 1.10
    if s == "likely_uninsured":
        return 0.85
    return 1.0


def _payment_factor(dbt_flag: Optional[str]) -> float:
    if not dbt_flag:
        return 1.0
    s = dbt_flag.lower()
    if s == "on_time":
        return 1.10
    if s == "late":
        return 0.70
    if s == "severely_late":
        return 0.40
    return 1.0


def _condition_factor(condition: Optional[str]) -> float:
    if not condition:
        return 1.0
    s = condition.lower()
    return {"excellent": 1.15, "good": 1.05, "watch": 0.75, "distress": 0.35, "unknown": 1.0}.get(s, 1.0)


def suggest_trade_credit_limit(
    *,
    latest_figures: Optional[ExtractedFigures],
    latest_ratios: Optional[RatiosPack] = None,
    balance_verdict: Optional[BalanceVerdict] = None,
    insurance_state: Optional[str] = None,
    payment_dbt: Optional[str] = None,
) -> LimitSuggestion:
    """Compute a recommended trade credit limit in PLN.

    All inputs are optional — the function degrades gracefully when data is
    incomplete, but returns ``recommended=None`` (still with a rationale) when
    there isn't enough balance data to compute any baseline.
    """
    out = LimitSuggestion()

    if latest_figures is None or (latest_figures.equity is None and latest_figures.revenue is None):
        out.rationale = (
            "Brak danych o kapitale własnym i przychodach — limit kupiecki nie może zostać oszacowany. "
            "Zasugeruj niewielki limit 5-10 tys. zł po manualnej ocenie."
        )
        return out

    equity = latest_figures.equity or 0.0
    revenue = latest_figures.revenue or 0.0

    equity_component = max(0.0, 0.10 * equity)
    revenue_component = max(0.0, 0.05 * revenue)
    base_candidates = [v for v in (equity_component, revenue_component) if v > 0]
    if not base_candidates:
        out.rationale = "Ujemny lub zerowy kapitał własny i brak przychodów — nie rekomendujemy kredytu kupieckiego."
        return out
    base = min(base_candidates)

    liq_f = _liquidity_factor(latest_ratios.current_ratio if latest_ratios else None)
    alt_f = _altman_factor(latest_ratios.altman_z_em if latest_ratios else None)
    ins_f = _insurance_factor(insurance_state)
    pay_f = _payment_factor(payment_dbt)
    cond_f = _condition_factor(balance_verdict.condition if balance_verdict else None)

    multiplier = _clip(liq_f * alt_f * ins_f * pay_f * cond_f, 0.05, 2.5)
    limit = base * multiplier

    # Round to human-readable buckets.
    if limit >= 1_000_000:
        limit = round(limit / 50_000) * 50_000
    elif limit >= 100_000:
        limit = round(limit / 10_000) * 10_000
    elif limit >= 10_000:
        limit = round(limit / 1_000) * 1_000
    else:
        limit = round(limit / 500) * 500

    out.recommended = limit
    out.low = round(limit * 0.6)
    out.high = round(limit * 1.4)

    # Human-readable rationale
    bits: list[str] = [
        f"Baza: min(10% kapitału własnego, 5% przychodów) = {base:,.0f} PLN.",
        f"Mnożnik: płynność {liq_f:.2f} × Altman {alt_f:.2f} × "
        f"ubezpieczenie {ins_f:.2f} × płatności {pay_f:.2f} × kondycja {cond_f:.2f} "
        f"= {multiplier:.2f}.",
    ]
    if balance_verdict and balance_verdict.condition != "unknown":
        bits.append(f"Werdykt bilansu: {balance_verdict.condition}.")
    if insurance_state and insurance_state != "unknown":
        bits.append(f"Sygnał ubezpieczenia: {insurance_state}.")
    if payment_dbt and payment_dbt != "unknown":
        bits.append(f"Opinia płatnicza: {payment_dbt}.")

    out.rationale = " ".join(bits)
    out.factors = {
        "base_pln": round(base, 2),
        "liquidity_factor": liq_f,
        "altman_factor": alt_f,
        "insurance_factor": ins_f,
        "payment_factor": pay_f,
        "condition_factor": cond_f,
        "multiplier": round(multiplier, 3),
        "equity_component_pln": round(equity_component, 2),
        "revenue_component_pln": round(revenue_component, 2),
    }
    return out
