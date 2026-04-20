"""Score contract activity for a company.

Computes a 0..100 intensity score (higher = riskier/inactive) from:

* count of contracts over the last 12 months vs prior 12 months
* total awarded value over the same windows
* counterparty diversification (Herfindahl-Hirschman Index, HHI)
* share of public vs private (news) contracts

Used as one of the inputs into the Commercial pillar of the composite score
and surfaced on the UI's "Kontraktacja" tab.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import Contract


@dataclass
class ContractIntensity:
    score: float = 50.0                   # 0..100, higher = riskier (less activity)
    active_count: int = 0
    last_12m_count: int = 0
    prior_12m_count: int = 0
    last_12m_value_pln: float = 0.0
    prior_12m_value_pln: float = 0.0
    count_yoy: float | None = None       # fraction
    value_yoy: float | None = None       # fraction
    hhi: float | None = None             # 0..1, >0.25 concentrated
    public_share: float | None = None    # 0..1, TED+BZP / all
    red_flags: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_contract_intensity(contracts: list[Contract]) -> ContractIntensity:
    now = datetime.now(timezone.utc)
    w12 = now - timedelta(days=365)
    w24 = now - timedelta(days=730)

    last_window: list[Contract] = []
    prior_window: list[Contract] = []
    for c in contracts:
        award = c.award_date or c.detected_at
        if not award:
            continue
        if award.tzinfo is None:
            award = award.replace(tzinfo=timezone.utc)
        if award >= w12:
            last_window.append(c)
        elif award >= w24:
            prior_window.append(c)

    last_value = sum((c.value_pln or 0.0) for c in last_window)
    prior_value = sum((c.value_pln or 0.0) for c in prior_window)

    count_yoy: float | None = None
    if prior_window:
        count_yoy = (len(last_window) - len(prior_window)) / max(1, len(prior_window))
    value_yoy: float | None = None
    if prior_value > 0:
        value_yoy = (last_value - prior_value) / prior_value

    # HHI on counterparty names (only where we have them).
    weights: dict[str, float] = {}
    total_weight = 0.0
    for c in last_window:
        key = (c.counterparty or c.source or "").lower().strip()[:200]
        if not key:
            continue
        w = c.value_pln if c.value_pln and c.value_pln > 0 else 1.0
        weights[key] = weights.get(key, 0.0) + w
        total_weight += w
    hhi: float | None = None
    if total_weight > 0 and weights:
        hhi = sum((v / total_weight) ** 2 for v in weights.values())

    public_share: float | None = None
    if last_window:
        pub = sum(1 for c in last_window if c.source in ("TED", "BZP", "GPW_ESPI"))
        public_share = pub / len(last_window)

    # ── Build 0..100 score (higher = worse) ─────────────────────────
    score = 50.0
    red_flags: list[str] = []
    positives: list[str] = []

    if count_yoy is not None:
        if count_yoy <= -0.40:
            score += 20
            red_flags.append(f"Spadek liczby kontraktów r/r {count_yoy:.0%} — sygnał kurczącej się sprzedaży.")
        elif count_yoy <= -0.15:
            score += 10
        elif count_yoy >= 0.20:
            score -= 10
            positives.append(f"Wzrost liczby kontraktów r/r {count_yoy:.0%}.")
    if value_yoy is not None:
        if value_yoy <= -0.40:
            score += 15
            red_flags.append(f"Spadek wartości kontraktów r/r {value_yoy:.0%}.")
        elif value_yoy <= -0.15:
            score += 8
        elif value_yoy >= 0.20:
            score -= 8
            positives.append(f"Wzrost wartości kontraktów r/r {value_yoy:.0%}.")

    if hhi is not None:
        if hhi > 0.40:
            score += 15
            red_flags.append(f"Wysoka koncentracja kontrahentów (HHI {hhi:.2f}) — ryzyko utraty kluczowego klienta.")
        elif hhi > 0.25:
            score += 7

    if len(last_window) == 0 and len(prior_window) == 0:
        # No signal at all — stay neutral with a note rather than inflate risk.
        score = 50.0
        red_flags.append("Brak publicznych kontraktów w bazie — ocena ograniczona.")
    elif len(last_window) == 0:
        score += 20
        red_flags.append("Brak nowych kontraktów w ostatnich 12m pomimo aktywności w poprzednim okresie.")
    else:
        if len(last_window) >= 10:
            positives.append(f"{len(last_window)} aktywnych/nowych kontraktów w ostatnich 12m.")
            score -= 5

    score = _clip(score, 0.0, 100.0)

    return ContractIntensity(
        score=round(score, 1),
        active_count=sum(1 for c in contracts if (c.status or "").lower() in ("active", "reported")),
        last_12m_count=len(last_window),
        prior_12m_count=len(prior_window),
        last_12m_value_pln=round(last_value, 2),
        prior_12m_value_pln=round(prior_value, 2),
        count_yoy=count_yoy,
        value_yoy=value_yoy,
        hhi=hhi,
        public_share=public_share,
        red_flags=red_flags[:5],
        positives=positives[:4],
    )
