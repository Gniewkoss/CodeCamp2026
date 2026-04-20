"""Derived financial ratios + bankruptcy models + multi-year trend + health score.

Consumes ``ExtractedFigures`` produced by ``financial_extractor`` and yields a
``RatiosPack`` with everything downstream (Claude balance analyzer, trade credit
limit, composite scorer, UI) needs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.analysis.financial_extractor import ExtractedFigures

logger = logging.getLogger(__name__)


def _safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None:
        return None
    try:
        if den == 0:
            return None
        return float(num) / float(den)
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────────────────────
# Ratio dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass
class RatiosPack:
    period_end: str = ""

    current_ratio: Optional[float] = None
    quick_ratio: Optional[float] = None
    cash_ratio: Optional[float] = None
    debt_to_equity: Optional[float] = None
    debt_to_assets: Optional[float] = None
    roe: Optional[float] = None
    roa: Optional[float] = None
    net_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    asset_turnover: Optional[float] = None

    dpo: Optional[float] = None  # Days Payable Outstanding
    dso: Optional[float] = None  # Days Sales Outstanding
    dio: Optional[float] = None  # Days Inventory Outstanding
    cash_conversion_cycle: Optional[float] = None

    altman_z_em: Optional[float] = None
    maczynska_zem: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TrendPack:
    """Year-over-year deltas between the latest and prior periods."""

    revenue_yoy: Optional[float] = None          # fraction, e.g. 0.12 = +12%
    revenue_cagr_3y: Optional[float] = None
    profit_yoy: Optional[float] = None
    equity_yoy: Optional[float] = None
    assets_yoy: Optional[float] = None
    liabilities_yoy: Optional[float] = None
    solvency_trend: str = "unknown"              # improving | stable | deteriorating | unknown

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ────────────────────────────────────────────────────────────────────────
# Core ratio computation
# ────────────────────────────────────────────────────────────────────────


def compute_ratios(f: ExtractedFigures) -> RatiosPack:
    r = RatiosPack(period_end=f.period_end)

    r.current_ratio = _safe_div(f.current_assets, f.current_liabilities)
    r.quick_ratio = _safe_div(
        (f.current_assets or 0) - (f.inventory or 0) if f.current_assets is not None else None,
        f.current_liabilities,
    )
    r.cash_ratio = _safe_div(f.cash, f.current_liabilities)

    r.debt_to_equity = _safe_div(f.total_liabilities, f.equity)
    r.debt_to_assets = _safe_div(f.total_liabilities, f.total_assets)

    r.roe = _safe_div(f.net_profit, f.equity)
    r.roa = _safe_div(f.net_profit, f.total_assets)
    r.net_margin = _safe_div(f.net_profit, f.revenue)
    # Operating margin uses EBIT when available, else falls back to revenue - operating_costs.
    if f.revenue:
        if f.ebit is not None:
            r.operating_margin = _safe_div(f.ebit, f.revenue)
        elif f.operating_costs is not None:
            r.operating_margin = _safe_div(f.revenue - f.operating_costs, f.revenue)
    r.asset_turnover = _safe_div(f.revenue, f.total_assets)

    # Days-based ratios (standard DPO / DSO / DIO definitions, × 365).
    if f.operating_costs and f.trade_payables:
        r.dpo = 365.0 * f.trade_payables / f.operating_costs
    elif f.cost_of_revenue and f.trade_payables:
        r.dpo = 365.0 * f.trade_payables / f.cost_of_revenue

    if f.revenue and f.receivables:
        r.dso = 365.0 * f.receivables / f.revenue

    if f.cost_of_revenue and f.inventory:
        r.dio = 365.0 * f.inventory / f.cost_of_revenue
    elif f.operating_costs and f.inventory:
        r.dio = 365.0 * f.inventory / f.operating_costs

    if r.dso is not None and r.dio is not None and r.dpo is not None:
        r.cash_conversion_cycle = r.dso + r.dio - r.dpo

    r.altman_z_em = altman_z_em(f)
    r.maczynska_zem = maczynska_zem(f)

    return r


# ────────────────────────────────────────────────────────────────────────
# Bankruptcy models
# ────────────────────────────────────────────────────────────────────────


def altman_z_em(f: ExtractedFigures) -> Optional[float]:
    """Altman Z"-score EM model for emerging-market & non-listed private firms:

        Z" = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4

    where
        X1 = working_capital / total_assets
        X2 = retained_earnings / total_assets
        X3 = EBIT / total_assets
        X4 = book_equity / total_liabilities

    Interpretation (EM scale):
        Z" > 2.60  → safe
        1.10 ≤ Z" ≤ 2.60 → grey zone
        Z" < 1.10  → distress
    """
    if not (f.total_assets and f.total_assets > 0):
        return None
    working_capital = None
    if f.current_assets is not None and f.current_liabilities is not None:
        working_capital = f.current_assets - f.current_liabilities
    ebit = f.ebit
    equity = f.equity
    liab = f.total_liabilities
    if working_capital is None or ebit is None or equity is None or not liab or liab <= 0:
        return None
    x1 = working_capital / f.total_assets
    x2 = (f.retained_earnings or 0.0) / f.total_assets
    x3 = ebit / f.total_assets
    x4 = equity / liab
    return round(6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4, 3)


def maczynska_zem(f: ExtractedFigures) -> Optional[float]:
    """Mączyńska ZEM model — tuned for Polish companies.

        ZEM = 1.5·M1 + 0.08·M2 + 10·M3 + 5·M4 + 0.3·M5 + 0.1·M6

    where
        M1 = (net_profit + depreciation) / total_liabilities
        M2 = total_assets / total_liabilities
        M3 = net_profit / total_assets
        M4 = net_profit / revenue
        M5 = inventory / revenue
        M6 = revenue / total_assets

    Depreciation is not always broken out separately in our simplified
    extractor — we approximate (EBITDA − EBIT) when both are present, else 0.

    Interpretation:
        ZEM ≥ 2  → very good
        1 ≤ ZEM < 2 → good
        0 ≤ ZEM < 1 → weak
        ZEM < 0   → bankruptcy threat
    """
    if not (f.total_assets and f.total_assets > 0):
        return None
    if not (f.total_liabilities and f.total_liabilities > 0):
        return None
    if f.net_profit is None or not (f.revenue and f.revenue > 0):
        return None
    depreciation = 0.0
    if f.ebitda is not None and f.ebit is not None:
        depreciation = max(0.0, f.ebitda - f.ebit)
    m1 = (f.net_profit + depreciation) / f.total_liabilities
    m2 = f.total_assets / f.total_liabilities
    m3 = f.net_profit / f.total_assets
    m4 = f.net_profit / f.revenue
    m5 = (f.inventory or 0.0) / f.revenue
    m6 = f.revenue / f.total_assets
    return round(1.5 * m1 + 0.08 * m2 + 10 * m3 + 5 * m4 + 0.3 * m5 + 0.1 * m6, 3)


# ────────────────────────────────────────────────────────────────────────
# Multi-year trend
# ────────────────────────────────────────────────────────────────────────


def compute_trend(figures_by_year: list[ExtractedFigures]) -> TrendPack:
    """Expects figures sorted newest → oldest."""
    t = TrendPack()
    if len(figures_by_year) < 2:
        return t

    latest = figures_by_year[0]
    prior = figures_by_year[1]

    def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None or b == 0:
            return None
        return (a - b) / abs(b)

    t.revenue_yoy = _delta(latest.revenue, prior.revenue)
    t.profit_yoy = _delta(latest.net_profit, prior.net_profit)
    t.equity_yoy = _delta(latest.equity, prior.equity)
    t.assets_yoy = _delta(latest.total_assets, prior.total_assets)
    t.liabilities_yoy = _delta(latest.total_liabilities, prior.total_liabilities)

    # 3-year CAGR if we have a third point.
    if len(figures_by_year) >= 3:
        oldest = figures_by_year[2]
        if latest.revenue and oldest.revenue and oldest.revenue > 0:
            years_span = 2  # latest vs oldest-of-three is 2 periods apart
            try:
                t.revenue_cagr_3y = (latest.revenue / oldest.revenue) ** (1 / years_span) - 1
            except (ValueError, ZeroDivisionError):
                t.revenue_cagr_3y = None

    # Solvency trend — compare debt-to-assets at latest vs prior.
    d_latest = _safe_div(latest.total_liabilities, latest.total_assets)
    d_prior = _safe_div(prior.total_liabilities, prior.total_assets)
    if d_latest is not None and d_prior is not None:
        if d_latest < d_prior - 0.03:
            t.solvency_trend = "improving"
        elif d_latest > d_prior + 0.03:
            t.solvency_trend = "deteriorating"
        else:
            t.solvency_trend = "stable"

    return t


# ────────────────────────────────────────────────────────────────────────
# Financial health score (0..100, higher = riskier)
# ────────────────────────────────────────────────────────────────────────


@dataclass
class HealthBreakdown:
    score: float = 0.0                  # 0..100 (higher = worse)
    condition: str = "unknown"          # excellent | good | watch | distress | unknown
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "condition": self.condition,
            "components": self.components,
            "reasons": self.reasons,
        }


def financial_health_score(
    ratios_by_year: list[RatiosPack],
    *,
    trend: Optional[TrendPack] = None,
) -> HealthBreakdown:
    """Combine ratios, Altman, Mączyńska and trend into a 0..100 risk score.

    Intuition:
      * Safe Altman + good Mączyńska + healthy liquidity → score near 10–20.
      * Distressed Altman + negative Mączyńska + poor liquidity → score 80+.
      * Missing data moves the score toward a neutral 50 and downgrades the
        condition to ``unknown``.
    """
    result = HealthBreakdown()

    if not ratios_by_year:
        result.score = 50.0
        result.condition = "unknown"
        result.reasons.append("Brak danych finansowych — nie można ocenić kondycji.")
        return result

    latest = ratios_by_year[0]
    reasons: list[str] = []

    # --- Altman contribution (0..35) --------------------------------------
    alt_pts: Optional[float] = None
    if latest.altman_z_em is not None:
        if latest.altman_z_em >= 2.60:
            alt_pts = 5.0
            reasons.append(f"Altman Z\" {latest.altman_z_em:.2f} — strefa bezpieczna.")
        elif latest.altman_z_em >= 1.10:
            # Linear in grey zone: 2.60→5, 1.10→25
            alt_pts = 5.0 + 20.0 * (2.60 - latest.altman_z_em) / 1.50
            reasons.append(f"Altman Z\" {latest.altman_z_em:.2f} — strefa szara.")
        else:
            # Below 1.10 → distress, grows with how deep
            alt_pts = min(35.0, 25.0 + (1.10 - latest.altman_z_em) * 10.0)
            reasons.append(f"Altman Z\" {latest.altman_z_em:.2f} — strefa dystresu.")

    # --- Mączyńska contribution (0..25) -----------------------------------
    mz_pts: Optional[float] = None
    if latest.maczynska_zem is not None:
        if latest.maczynska_zem >= 2.0:
            mz_pts = 2.0
            reasons.append(f"Mączyńska ZEM {latest.maczynska_zem:.2f} — bardzo dobra kondycja.")
        elif latest.maczynska_zem >= 1.0:
            mz_pts = 8.0
            reasons.append(f"Mączyńska ZEM {latest.maczynska_zem:.2f} — dobra kondycja.")
        elif latest.maczynska_zem >= 0.0:
            mz_pts = 16.0
            reasons.append(f"Mączyńska ZEM {latest.maczynska_zem:.2f} — słaba kondycja.")
        else:
            mz_pts = min(25.0, 20.0 + abs(latest.maczynska_zem) * 2.0)
            reasons.append(f"Mączyńska ZEM {latest.maczynska_zem:.2f} — zagrożenie upadłością.")

    # --- Liquidity contribution (0..20) -----------------------------------
    liq_pts: Optional[float] = None
    if latest.current_ratio is not None:
        if latest.current_ratio >= 1.5:
            liq_pts = 2.0
        elif latest.current_ratio >= 1.0:
            liq_pts = 8.0
            reasons.append(f"Płynność bieżąca {latest.current_ratio:.2f} — na granicy.")
        else:
            liq_pts = min(20.0, 12.0 + (1.0 - latest.current_ratio) * 15.0)
            reasons.append(f"Płynność bieżąca {latest.current_ratio:.2f} — poniżej 1.")

    # --- Leverage contribution (0..10) ------------------------------------
    lev_pts: Optional[float] = None
    if latest.debt_to_equity is not None:
        if latest.debt_to_equity < 1.0:
            lev_pts = 1.0
        elif latest.debt_to_equity < 2.0:
            lev_pts = 5.0
        else:
            lev_pts = min(10.0, 5.0 + (latest.debt_to_equity - 2.0) * 2.0)
            reasons.append(f"Dźwignia D/E {latest.debt_to_equity:.2f} — wysokie zadłużenie.")

    # --- Trend contribution (0..10) ---------------------------------------
    trend_pts: Optional[float] = None
    if trend is not None:
        trend_pts = 5.0  # neutral baseline
        if trend.revenue_yoy is not None:
            if trend.revenue_yoy > 0.05:
                trend_pts -= 2.0
            elif trend.revenue_yoy < -0.15:
                trend_pts += 3.0
                reasons.append(f"Przychody YoY {trend.revenue_yoy:.0%} — silny spadek sprzedaży.")
        if trend.solvency_trend == "improving":
            trend_pts -= 1.5
        elif trend.solvency_trend == "deteriorating":
            trend_pts += 2.0
            reasons.append("Trend wypłacalności: pogarsza się.")
        trend_pts = max(0.0, min(10.0, trend_pts))

    # --- Aggregate ---------------------------------------------------------
    component_map = {
        "altman": alt_pts,
        "maczynska": mz_pts,
        "liquidity": liq_pts,
        "leverage": lev_pts,
        "trend": trend_pts,
    }

    # If we have no insight at all, keep score neutral.
    known_values = [v for v in component_map.values() if v is not None]
    if not known_values:
        result.score = 50.0
        result.condition = "unknown"
        result.reasons.append("Brak wskaźników — zbyt skąpe dane bilansowe.")
        return result

    # Sum known components, then rescale for the max weight of known components.
    component_weights = {"altman": 35.0, "maczynska": 25.0, "liquidity": 20.0, "leverage": 10.0, "trend": 10.0}
    total_weight_known = sum(w for k, w in component_weights.items() if component_map[k] is not None)
    raw_total = sum(v for v in known_values)
    # Rescale to 100 coverage, then cap at 100.
    if total_weight_known > 0:
        raw_total = raw_total * 100.0 / total_weight_known
    score = max(0.0, min(100.0, raw_total))

    if score < 20:
        condition = "excellent"
    elif score < 40:
        condition = "good"
    elif score < 65:
        condition = "watch"
    else:
        condition = "distress"

    result.score = round(score, 1)
    result.condition = condition
    result.components = {k: round(v, 2) for k, v in component_map.items() if v is not None}
    result.reasons = reasons[:5]
    return result
