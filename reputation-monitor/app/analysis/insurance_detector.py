"""Multi-signal detector for trade-credit / liability insurance.

There is no public API for Polish trade-credit insurers (KUKE, Coface,
Allianz Trade / Euler Hermes, Atradius). We therefore aggregate THREE
best-effort signals:

1. Financial statement notes: if ``insurance_costs_mentioned`` was flagged
   during extraction, the company is booking meaningful insurance costs.
2. News-scan: existing ArticleAnalysis rows are searched for insurer-name
   co-occurrence ("KUKE", "Coface", "Allianz Trade", "Atradius",
   "Euler Hermes").
3. Claude knowledge fallback: ask the model if it knows whether the company
   is insured (medium confidence at best).

The output is an ``InsuranceDetection`` with a categorical state + source +
confidence, NOT a hard fact. The UI must show it as an estimate.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Article, ArticleAnalysis, FinancialFigures, FinancialStatement

logger = logging.getLogger(__name__)


INSURERS = [
    "KUKE",
    "Coface",
    "Allianz Trade",
    "Euler Hermes",
    "Atradius",
    "PZU",
    "Compensa",
    "TUiR Warta",
]


@dataclass
class InsuranceDetection:
    state: str = "unknown"                   # known_insured | likely_insured | unknown | likely_uninsured
    provider_guess: Optional[str] = None
    source: Optional[str] = None             # "financial_notes" | "news" | "claude_knowledge"
    confidence: float = 0.0                  # 0..1
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ────────────────────────────────────────────────────────────────────────
# Signal 1: financial notes
# ────────────────────────────────────────────────────────────────────────


def _check_financial_notes(db: Session, company_id: str) -> Optional[InsuranceDetection]:
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
    _, figures = row
    if figures.insurance_costs_mentioned:
        return InsuranceDetection(
            state="likely_insured",
            source="financial_notes",
            confidence=0.6,
            evidence=["Koszty ubezpieczeń wzmiankowane w sprawozdaniu finansowym."],
        )
    return None


# ────────────────────────────────────────────────────────────────────────
# Signal 2: news co-occurrence
# ────────────────────────────────────────────────────────────────────────


def _check_news_mentions(db: Session, company_id: str, *, company_name: str, aliases: list[str]) -> Optional[InsuranceDetection]:
    stmt = (
        select(Article, ArticleAnalysis)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .where(Article.company_id == company_id)
        .where(ArticleAnalysis.mentions_company.is_(True))
        .limit(60)
    )
    rows = db.execute(stmt).all()
    if not rows:
        return None

    needles = [company_name] + [a for a in aliases if a]
    name_re = re.compile("|".join(re.escape(n) for n in needles if n), re.IGNORECASE)
    provider_hits: dict[str, list[str]] = {}

    for art, an in rows:
        blob = " ".join(filter(None, [art.title or "", an.summary or "", an.raw_llm_response or ""]))
        if not blob or not name_re.search(blob):
            continue
        for ins in INSURERS:
            if re.search(rf"(?<!\w){re.escape(ins)}(?!\w)", blob, re.IGNORECASE):
                provider_hits.setdefault(ins, []).append((art.title or art.url or "")[:120])

    if not provider_hits:
        return None
    top_insurer, evidences = max(provider_hits.items(), key=lambda kv: len(kv[1]))
    return InsuranceDetection(
        state="likely_insured",
        provider_guess=top_insurer,
        source="news",
        confidence=0.55 + min(0.25, 0.05 * len(evidences)),
        evidence=[f"Artykuł: {e}" for e in evidences[:3]],
    )


# ────────────────────────────────────────────────────────────────────────
# Signal 3: Claude knowledge
# ────────────────────────────────────────────────────────────────────────


_KNOWLEDGE_SYSTEM = """Jesteś analitykiem due-diligence znającym rynek ubezpieczeń \
należności handlowych w Polsce. Odpowiadasz na pytanie: czy dana spółka posiada \
ubezpieczenie należności lub polisę OC działalności.

Zwróć STRICT JSON:
{
  "state": "known_insured" | "likely_insured" | "unknown" | "likely_uninsured",
  "provider_guess": "KUKE" | "Coface" | "Allianz Trade" | "Euler Hermes" | "Atradius" | "PZU" | null,
  "confidence": 0.0 .. 1.0,
  "evidence": ["krótki dowód w języku polskim", ...]
}

Zasady:
- "known_insured" tylko gdy pamiętasz KONKRETNIE, że spółka publicznie potwierdziła \
  ubezpieczenie należności (np. raporty roczne, wywiady, sprawozdania).
- "likely_insured" gdy spółka jest duża/eksportowa i niemal na pewno ma takie \
  ubezpieczenie, ale nie masz konkretnego potwierdzenia.
- "likely_uninsured" gdy to mała/lokalna firma bez skomplikowanej sieci odbiorców.
- Nie zmyślaj providerów — lepiej zostawić null.
"""


def _ask_claude(company_name: str, sector: Optional[str] = None) -> Optional[InsuranceDetection]:
    from app.llm import llm_available, llm_complete

    if not llm_available():
        return None
    raw = llm_complete(
        system=_KNOWLEDGE_SYSTEM,
        user=json.dumps({"company": company_name, "sector": sector}, ensure_ascii=False),
        max_tokens=500,
        purpose="insurance_detector",
    )
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None

    valid_states = {"known_insured", "likely_insured", "unknown", "likely_uninsured"}
    state = str(data.get("state") or "unknown").lower()
    if state not in valid_states:
        state = "unknown"
    evidence = data.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return InsuranceDetection(
        state=state,
        provider_guess=str(data.get("provider_guess") or "").strip() or None,
        source="claude_knowledge",
        confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.4))),
        evidence=[str(e).strip() for e in evidence if e][:3],
    )


# ────────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────────


def detect_insurance(
    db: Session,
    company_id: str,
    *,
    company_name: str,
    sector: Optional[str] = None,
    aliases: Optional[list[str]] = None,
) -> InsuranceDetection:
    """Aggregate all signals and return the strongest finding."""
    aliases = aliases or []
    candidates: list[InsuranceDetection] = []

    if (r := _check_financial_notes(db, company_id)) is not None:
        candidates.append(r)
    if (r := _check_news_mentions(db, company_id, company_name=company_name, aliases=aliases)) is not None:
        candidates.append(r)
    if (r := _ask_claude(company_name, sector=sector)) is not None:
        candidates.append(r)

    if not candidates:
        return InsuranceDetection(
            state="unknown",
            source=None,
            confidence=0.0,
            evidence=["Brak sygnałów o ubezpieczeniu należności w dostępnych danych."],
        )

    # Prefer findings in this order: known_insured > likely_insured > likely_uninsured > unknown.
    priority = {"known_insured": 3, "likely_insured": 2, "likely_uninsured": 1, "unknown": 0}
    best = max(candidates, key=lambda c: (priority.get(c.state, 0), c.confidence))

    # Merge evidence across candidates so the UI can show everything found.
    merged_evidence: list[str] = []
    merged_providers: list[str] = []
    for c in candidates:
        for e in c.evidence or []:
            if e not in merged_evidence:
                merged_evidence.append(e)
        if c.provider_guess and c.provider_guess not in merged_providers:
            merged_providers.append(c.provider_guess)
    best.evidence = merged_evidence[:5]
    if not best.provider_guess and merged_providers:
        best.provider_guess = merged_providers[0]
    return best
