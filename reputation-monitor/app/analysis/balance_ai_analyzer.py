"""Dedicated Claude prompt analysing 3 years of balance sheet + ratios.

This is the hero module of the multi-stage financial analysis: we hand Claude a
structured JSON snapshot of the company's 3 most recent fiscal years and ask
for an economist-grade verdict (condition, red flags, strengths, near-term and
long-term risks, plain-language commentary, 12-month solvency forecast).

The result is persisted into ``FinancialAIAnalysis`` and fed into the composite
risk score via the ``Financial`` pillar.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from app.analysis.financial_extractor import ExtractedFigures
from app.analysis.financial_metrics import RatiosPack, TrendPack
from app.config import get_settings

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Output dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass
class BalanceVerdict:
    condition: str = "unknown"      # excellent | good | watch | distress | unknown
    red_flags: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    short_term_risks: list[str] = field(default_factory=list)
    long_term_risks: list[str] = field(default_factory=list)
    commentary: str = ""
    solvency_forecast_12m: str = "unknown"   # low | medium | high
    years_covered: list[str] = field(default_factory=list)
    raw_prompt: Optional[str] = None
    raw_response: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k not in ("raw_prompt", "raw_response")}


# ────────────────────────────────────────────────────────────────────────
# Prompt
# ────────────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """Jesteś doświadczonym analitykiem finansowym oceniającym spółkę jako \
potencjalnego kontrahenta B2B dla klienta z Polski. Dostajesz do 3 lat sprawozdań \
finansowych (wybrane pozycje bilansu + RZiS), wyliczone wskaźniki oraz trendy. \
Twoim zadaniem jest rzetelna ocena kondycji ekonomicznej i zdolności \
regulowania zobowiązań w perspektywie 12 miesięcy.

ZASADY:
• Oceniaj rzetelnie i bez marketingu — masz pomóc klientowi uniknąć strat.
• Uwzględniaj polski kontekst makroekonomiczny (inflacja, stopy, PKD branży).
• Rozróżniaj sygnały krótkoterminowe (płynność, DPO, cykl konwersji gotówki) \
  od długoterminowych (dźwignia, struktura kapitału, trend kapitału własnego).
• Czerwone flagi identyfikuj na podstawie KONKRETNYCH liczb/trendów z danych.
• Mocne strony również z danych — nie generyczne hasła.
• Komentarz ekonomiczny 3-5 zdań po polsku, naturalnym językiem analityka.
• Prognoza wypłacalności 12m: low (bezpieczny), medium (wymaga monitoringu), \
  high (realne ryzyko niewypłacalności).
• Jeśli dane są zbyt skąpe aby ocenić → condition="unknown", \
  solvency_forecast_12m="medium", w commentary wyjaśnij czego brakuje.

Zwracaj WYŁĄCZNIE JSON zgodny ze schematem, bez markdown i bez prozy.

SCHEMA:
{
  "condition": "excellent" | "good" | "watch" | "distress" | "unknown",
  "red_flags": [ "konkretny sygnał oparty o liczby, po polsku", ... 3-5 items ],
  "strengths": [ "konkretna mocna strona, po polsku", ... 2-5 items ],
  "short_term_risks": [ "ryzyka do 12 miesięcy", ... 0-5 items ],
  "long_term_risks": [ "ryzyka 12m+", ... 0-5 items ],
  "commentary": "3-5 zdań po polsku, ekonomicznym językiem",
  "solvency_forecast_12m": "low" | "medium" | "high"
}"""


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _slim_figures(f: ExtractedFigures) -> dict[str, Any]:
    """Trim ExtractedFigures to fields that matter for the analyst — keeps tokens low."""
    return {
        "period_end": f.period_end,
        "period_type": f.period_type,
        "currency": f.currency,
        "source": f.source,
        "confidence": f.confidence,
        "revenue": f.revenue,
        "operating_costs": f.operating_costs,
        "ebit": f.ebit,
        "net_profit": f.net_profit,
        "total_assets": f.total_assets,
        "current_assets": f.current_assets,
        "non_current_assets": f.non_current_assets,
        "cash": f.cash,
        "inventory": f.inventory,
        "receivables": f.receivables,
        "total_liabilities": f.total_liabilities,
        "current_liabilities": f.current_liabilities,
        "non_current_liabilities": f.non_current_liabilities,
        "trade_payables": f.trade_payables,
        "equity": f.equity,
        "retained_earnings": f.retained_earnings,
    }


def _slim_ratios(r: RatiosPack) -> dict[str, Any]:
    return {k: v for k, v in r.as_dict().items() if v is not None}


def _coerce_list(raw: Any, limit: int) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if not x:
            continue
        s = str(x).strip()
        if s:
            out.append(s[:280])
        if len(out) >= limit:
            break
    return out


def _coerce_verdict(data: dict) -> BalanceVerdict:
    valid_conditions = {"excellent", "good", "watch", "distress", "unknown"}
    valid_forecasts = {"low", "medium", "high"}
    condition = str(data.get("condition") or "unknown").lower().strip()
    if condition not in valid_conditions:
        condition = "unknown"
    forecast = str(data.get("solvency_forecast_12m") or "").lower().strip()
    if forecast not in valid_forecasts:
        forecast = "medium" if condition not in ("excellent", "good") else "low"
    return BalanceVerdict(
        condition=condition,
        red_flags=_coerce_list(data.get("red_flags"), limit=6),
        strengths=_coerce_list(data.get("strengths"), limit=6),
        short_term_risks=_coerce_list(data.get("short_term_risks"), limit=6),
        long_term_risks=_coerce_list(data.get("long_term_risks"), limit=6),
        commentary=str(data.get("commentary") or "").strip()[:1200],
        solvency_forecast_12m=forecast,
    )


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────


def analyze_balance_sheet(
    *,
    company_name: str,
    nip: Optional[str] = None,
    sector: Optional[str] = None,
    figures_by_year: list[ExtractedFigures],
    ratios_by_year: list[RatiosPack],
    trend: Optional[TrendPack] = None,
    health_score: Optional[float] = None,
) -> BalanceVerdict:
    """Run the dedicated Claude analyst on up to 3 years of data.

    Both input lists must be sorted newest → oldest. If anthropic isn't
    configured or data is insufficient, returns an informative unknown verdict
    with a heuristic commentary so the UI still has something to show.
    """
    settings = get_settings()

    years_covered = [f.period_end for f in figures_by_year[:3] if f.period_end]

    if not figures_by_year:
        return BalanceVerdict(
            condition="unknown",
            commentary=(
                f"Brak sprawozdań finansowych dla {company_name}. Nie można ocenić kondycji "
                "ekonomicznej bez danych bilansowych."
            ),
            solvency_forecast_12m="medium",
            years_covered=years_covered,
        )

    payload = {
        "company": {"name": company_name, "nip": nip, "sector": sector},
        "years": [
            {
                "figures": _slim_figures(f),
                "ratios": _slim_ratios(r) if r is not None else None,
            }
            for f, r in zip(figures_by_year[:3], ratios_by_year[:3])
        ],
        "trend": trend.as_dict() if trend else None,
        "heuristic_health_score": health_score,
    }
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    from app.llm import llm_available, llm_complete

    if not llm_available():
        return _heuristic_fallback(payload, figures_by_year, years_covered)

    raw = llm_complete(
        system=_SYSTEM_PROMPT,
        user=payload_json[:18000],
        max_tokens=1800,
        purpose="balance_analyzer",
    )
    if not raw:
        verdict = _heuristic_fallback(payload, figures_by_year, years_covered)
        verdict.raw_prompt = payload_json[:4000]
        return verdict

    data = _extract_json(raw)
    if not data:
        logger.info("Balance analyzer: non-JSON reply: %s", raw[:300])
        verdict = _heuristic_fallback(payload, figures_by_year, years_covered)
        verdict.raw_response = raw
        verdict.raw_prompt = payload_json[:4000]
        return verdict
    verdict = _coerce_verdict(data)
    verdict.years_covered = years_covered
    verdict.raw_prompt = payload_json[:4000]
    verdict.raw_response = raw[:4000]
    return verdict


# ────────────────────────────────────────────────────────────────────────
# Heuristic fallback used when the LLM is unavailable
# ────────────────────────────────────────────────────────────────────────


def _heuristic_fallback(
    payload: dict,
    figures_by_year: list[ExtractedFigures],
    years_covered: list[str],
) -> BalanceVerdict:
    latest = figures_by_year[0]
    rs: list[str] = []
    fl: list[str] = []
    trend = payload.get("trend") or {}

    if latest.equity is not None and latest.equity < 0:
        fl.append("Ujemny kapitał własny — spółka pokazuje stratę bilansową przewyższającą kapitały.")
    if latest.current_assets and latest.current_liabilities and latest.current_liabilities > 0:
        cr = latest.current_assets / latest.current_liabilities
        if cr < 1.0:
            fl.append(f"Płynność bieżąca {cr:.2f} poniżej 1.0 — ryzyko krótkoterminowej niewypłacalności.")
        elif cr > 1.5:
            rs.append(f"Płynność bieżąca {cr:.2f} — komfortowa rezerwa aktywów obrotowych.")
    if latest.net_profit is not None and latest.net_profit > 0:
        rs.append("Spółka generuje dodatni wynik netto.")
    if latest.net_profit is not None and latest.net_profit < 0:
        fl.append("Strata netto w najnowszym okresie.")
    if trend.get("revenue_yoy") is not None:
        yoy = float(trend["revenue_yoy"])
        if yoy < -0.15:
            fl.append(f"Przychody spadły r/r o {yoy*100:.0f}% — silny sygnał osłabienia biznesu.")
        elif yoy > 0.1:
            rs.append(f"Przychody wzrosły r/r o {yoy*100:.0f}% — dynamiczny wzrost.")

    if fl and not rs:
        condition = "distress"
        forecast = "high"
    elif fl:
        condition = "watch"
        forecast = "medium"
    elif rs:
        condition = "good"
        forecast = "low"
    else:
        condition = "unknown"
        forecast = "medium"

    commentary = (
        "Analiza offline (bez Claude): werdykt na bazie prostych reguł. "
        + (" ".join(rs + fl) if (rs or fl) else "Zbyt mało danych do szczegółowej oceny.")
    )
    return BalanceVerdict(
        condition=condition,
        red_flags=fl[:5],
        strengths=rs[:5],
        short_term_risks=fl[:3],
        long_term_risks=[],
        commentary=commentary[:1000],
        solvency_forecast_12m=forecast,
        years_covered=years_covered,
    )
