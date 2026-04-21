"""Extract structured financial figures from sprawozdanie finansowe sources.

Three paths, tried in order:

1. Deterministic mapping from e-SF JSON (structured XBRL-like from KRS RDF).
2. Claude extraction from PDF / HTML statement text.
3. Claude knowledge fallback — ask the model to recall publicly known figures
   for the given Polish company, used when no sprawozdanie is attached.
   Marked as ``source="CLAUDE_KNOWLEDGE"`` and flagged in the UI so the user
   knows the numbers are estimated.

All paths converge on the same ``ExtractedFigures`` dataclass which downstream
modules (``financial_metrics``, ``balance_ai_analyzer``) can consume.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Data model
# ────────────────────────────────────────────────────────────────────────


@dataclass
class ExtractedFigures:
    period_end: str = ""  # "YYYY-MM-DD"
    period_type: str = "annual"
    currency: str = "PLN"

    revenue: Optional[float] = None
    cost_of_revenue: Optional[float] = None
    operating_costs: Optional[float] = None
    ebit: Optional[float] = None
    ebitda: Optional[float] = None
    net_profit: Optional[float] = None

    total_assets: Optional[float] = None
    current_assets: Optional[float] = None
    non_current_assets: Optional[float] = None
    cash: Optional[float] = None
    inventory: Optional[float] = None
    receivables: Optional[float] = None

    total_liabilities: Optional[float] = None
    current_liabilities: Optional[float] = None
    non_current_liabilities: Optional[float] = None
    trade_payables: Optional[float] = None
    equity: Optional[float] = None
    retained_earnings: Optional[float] = None

    cash_from_operations: Optional[float] = None
    capex: Optional[float] = None

    insurance_costs_mentioned: Optional[bool] = None

    source: str = "UNKNOWN"  # KRS_RDF | CLAUDE_PDF | CLAUDE_KNOWLEDGE | MANUAL
    confidence: float = 0.6  # 0..1
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    def is_balance_sheet_consistent(self, tolerance: float = 0.03) -> bool:
        """Check whether total_assets ≈ total_liabilities + equity within tolerance."""
        if self.total_assets is None or self.equity is None:
            return False
        liab = self.total_liabilities or 0.0
        target = liab + self.equity
        if self.total_assets == 0:
            return target == 0
        return abs(self.total_assets - target) / abs(self.total_assets) <= tolerance

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────────
# Path 1 — deterministic e-SF JSON mapping
# ────────────────────────────────────────────────────────────────────────

# The Polish e-SF (elektroniczne sprawozdanie finansowe) schema uses hierarchical
# keys. We support the most common labels. Extractors walk the whole dict and
# match on any key containing one of the substrings — this is resilient to the
# nested structure produced by different vendors (Ministerstwo Finansów, e-KRS
# web form, accounting tools).

_KEY_HINTS: dict[str, list[str]] = {
    "revenue": [
        "przychody netto ze sprzedaży",
        "przychody ze sprzedaży",
        "przychody netto",
        "net revenue",
        "revenue",
    ],
    "cost_of_revenue": [
        "koszt własny sprzedaży",
        "koszt sprzedanych",
        "cost of sales",
    ],
    "operating_costs": [
        "koszty działalności operacyjnej",
        "koszty operacyjne",
        "operating expenses",
    ],
    "ebit": [
        "zysk z działalności operacyjnej",
        "wynik z działalności operacyjnej",
        "operating profit",
        "ebit",
    ],
    "net_profit": [
        "zysk netto",
        "wynik netto",
        "net profit",
        "net income",
    ],
    "total_assets": [
        "aktywa razem",
        "suma aktywów",
        "total assets",
    ],
    "current_assets": [
        "aktywa obrotowe",
        "current assets",
    ],
    "non_current_assets": [
        "aktywa trwałe",
        "non-current assets",
    ],
    "cash": [
        "środki pieniężne i ich ekwiwalenty",
        "środki pieniężne",
        "cash and cash equivalents",
        "cash",
    ],
    "inventory": [
        "zapasy",
        "inventory",
    ],
    "receivables": [
        "należności krótkoterminowe",
        "należności handlowe",
        "trade receivables",
    ],
    "total_liabilities": [
        "zobowiązania i rezerwy",
        "zobowiązania razem",
        "total liabilities",
    ],
    "current_liabilities": [
        "zobowiązania krótkoterminowe",
        "current liabilities",
    ],
    "non_current_liabilities": [
        "zobowiązania długoterminowe",
        "non-current liabilities",
    ],
    "trade_payables": [
        "zobowiązania z tytułu dostaw",
        "trade payables",
    ],
    "equity": [
        "kapitał własny",
        "kapitał (fundusz) własny",
        "total equity",
    ],
    "retained_earnings": [
        "zysk z lat ubiegłych",
        "zyski zatrzymane",
        "retained earnings",
    ],
}


def _walk_pairs(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Yield (dotted_path, value) pairs for every leaf in a nested structure."""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            new_path = f"{path}.{k}" if path else str(k)
            out.extend(_walk_pairs(v, new_path))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_walk_pairs(v, f"{path}[{i}]"))
    else:
        out.append((path, node))
    return out


def _match_figure(pairs: list[tuple[str, Any]], hints: list[str]) -> Optional[float]:
    lowered = [(p.lower(), v) for p, v in pairs]
    for hint in hints:
        h = hint.lower()
        for path, val in lowered:
            if h in path and isinstance(val, (int, float)):
                return float(val)
            if h in path and isinstance(val, str):
                num = _coerce_pln(val)
                if num is not None:
                    return num
    return None


def _coerce_pln(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", text)
    cleaned = cleaned.replace(" ", "").replace("\xa0", "")
    # Polish convention: "1 234 567,89" — commas are decimal, dots thousand sep.
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def from_esf_json(raw: dict[str, Any], *, period_end: str = "", period_type: str = "annual") -> ExtractedFigures:
    """Deterministic mapping from e-SF structured JSON."""
    pairs = _walk_pairs(raw)
    fig = ExtractedFigures(period_end=period_end, period_type=period_type, source="KRS_RDF", confidence=0.9)
    for field_name, hints in _KEY_HINTS.items():
        value = _match_figure(pairs, hints)
        if value is not None:
            setattr(fig, field_name, value)

    # Detect insurance cost mentions anywhere in the document.
    lowered = " ".join(str(p).lower() for p, _ in pairs)
    if any(kw in lowered for kw in ("ubezpieczen", "polisa ", "insurance")):
        fig.insurance_costs_mentioned = True

    if not fig.is_balance_sheet_consistent():
        fig.warnings.append("bilans niespójny: aktywa ≠ pasywa + kapitał")
        fig.confidence = min(fig.confidence, 0.6)

    return fig


# ────────────────────────────────────────────────────────────────────────
# Path 2 + 3 — Claude extraction / knowledge
# ────────────────────────────────────────────────────────────────────────


_EXTRACT_SYSTEM = """You are an expert Polish financial-statement data extractor.

You receive a sprawozdanie finansowe (balance sheet + income statement,
possibly incomplete) as free text. Extract the key figures into STRICT JSON.

Rules:
- All figures in PLN. Convert "tys. zł" → multiply by 1000, "mln zł" → × 1_000_000.
- Use negative numbers for losses / liabilities where the source clearly shows it
  as a loss (strata, ujemny).
- If a figure is not present in the text, set it to null — never guess.
- ``period_end`` is "YYYY-MM-DD" (end of reporting period, typically "-12-31").
- Do not include commentary — return ONLY the JSON object.

JSON schema:
{
  "period_end": "YYYY-MM-DD",
  "period_type": "annual" | "semi" | "quarterly",
  "currency": "PLN",
  "revenue": number | null,
  "operating_costs": number | null,
  "ebit": number | null,
  "net_profit": number | null,
  "total_assets": number | null,
  "current_assets": number | null,
  "non_current_assets": number | null,
  "cash": number | null,
  "inventory": number | null,
  "receivables": number | null,
  "total_liabilities": number | null,
  "current_liabilities": number | null,
  "non_current_liabilities": number | null,
  "trade_payables": number | null,
  "equity": number | null,
  "retained_earnings": number | null,
  "cash_from_operations": number | null,
  "capex": number | null,
  "insurance_costs_mentioned": true | false | null
}"""


_KNOWLEDGE_SYSTEM = """You are a senior financial analyst with deep knowledge of Polish
public and private companies. Given the name (and optionally NIP/KRS) of a well-known
Polish entity, recall publicly-available financial information from its filed
sprawozdania finansowe and produce a best-effort 3-year summary.

CRITICAL RULES:
- ONLY provide figures if you genuinely remember them from public filings
  (KRS repository, GPW ESPI reports, press releases, financial news).
- For each year return ``confidence`` (low | medium | high). Use "low"
  whenever you're estimating from context rather than recalling a specific
  number. Be honest — downstream systems weight this heavily.
- NEVER invent figures for smaller / lesser-known companies. If you don't
  genuinely know, return ``years: []`` and explain in ``notes``.
- All numbers in PLN (not thousands). Convert from USD/EUR if necessary.
- Years covered should be the 3 most recent closed fiscal years you can recall.

Return ONLY a JSON object with this shape:

{
  "years": [
    {
      "period_end": "YYYY-MM-DD",
      "period_type": "annual",
      "currency": "PLN",
      "confidence": "low" | "medium" | "high",
      "revenue": number | null,
      "operating_costs": number | null,
      "ebit": number | null,
      "net_profit": number | null,
      "total_assets": number | null,
      "current_assets": number | null,
      "non_current_assets": number | null,
      "cash": number | null,
      "inventory": number | null,
      "receivables": number | null,
      "total_liabilities": number | null,
      "current_liabilities": number | null,
      "non_current_liabilities": number | null,
      "trade_payables": number | null,
      "equity": number | null,
      "retained_earnings": number | null
    },
    ...
  ],
  "notes": "short explanation of data availability / caveats in Polish"
}"""


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


def _coerce_number(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        return _coerce_pln(x)
    return None


def _coerce_extracted(data: dict, *, default_source: str, default_confidence: float) -> ExtractedFigures:
    fig = ExtractedFigures(source=default_source, confidence=default_confidence)
    fig.period_end = str(data.get("period_end") or "")[:10]
    fig.period_type = str(data.get("period_type") or "annual")
    fig.currency = str(data.get("currency") or "PLN")
    for field_name in _KEY_HINTS.keys():
        fig_val = _coerce_number(data.get(field_name))
        if fig_val is not None:
            setattr(fig, field_name, fig_val)
    # Also handle retained_earnings and cash_from_operations / capex if present.
    for extra in ("cash_from_operations", "capex", "ebitda", "cost_of_revenue"):
        v = _coerce_number(data.get(extra))
        if v is not None:
            setattr(fig, extra, v)
    ins = data.get("insurance_costs_mentioned")
    if isinstance(ins, bool):
        fig.insurance_costs_mentioned = ins
    if not fig.is_balance_sheet_consistent():
        fig.warnings.append("bilans niespójny: aktywa ≠ pasywa + kapitał")
    return fig


def extract_from_text(text: str, *, period_hint: str = "") -> Optional[ExtractedFigures]:
    """LLM extraction from PDF/HTML text of a sprawozdanie."""
    from app.llm import llm_available, llm_complete

    if not llm_available() or not text:
        return None
    raw = llm_complete(
        system=_EXTRACT_SYSTEM,
        user=(
            f"PODPOWIEDŹ OKRESU: {period_hint or '(brak)'}\n\n"
            f"TEKST SPRAWOZDANIA:\n{text[:60000]}"
        ),
        max_tokens=1800,
        purpose="fin_extract_pdf",
    )
    if not raw:
        return None
    data = _extract_json(raw)
    if not data:
        logger.info("LLM PDF extract returned non-JSON: %s", raw[:300])
        return None
    return _coerce_extracted(data, default_source="CLAUDE_PDF", default_confidence=0.75)


def extract_from_knowledge(
    company_name: str,
    *,
    nip: Optional[str] = None,
    krs: Optional[str] = None,
    sector: Optional[str] = None,
    years: int = 3,
) -> list[ExtractedFigures]:
    """Ask Claude to recall publicly-available figures for a well-known PL company.

    Used as the demo/hackathon fallback when no sprawozdanie PDF is attached.
    Returns an empty list if the LLM can't genuinely recall (low-confidence
    responses are filtered out here).
    """
    from app.llm import llm_available, llm_complete

    if not llm_available():
        return []
    user = {
        "company": {"name": company_name, "nip": nip, "krs": krs, "sector": sector},
        "years_requested": years,
    }
    raw = llm_complete(
        system=_KNOWLEDGE_SYSTEM,
        user=json.dumps(user, ensure_ascii=False),
        max_tokens=2200,
        purpose="fin_extract_knowledge",
    )
    if not raw:
        return []
    data = _extract_json(raw)
    if not data:
        logger.info("LLM knowledge returned non-JSON: %s", raw[:300])
        return []

    out: list[ExtractedFigures] = []
    notes = str(data.get("notes") or "")
    for year_data in data.get("years") or []:
        if not isinstance(year_data, dict):
            continue
        confidence_raw = str(year_data.get("confidence") or "low").lower()
        cmap = {"low": 0.4, "medium": 0.65, "high": 0.85}
        fig = _coerce_extracted(
            year_data,
            default_source="CLAUDE_KNOWLEDGE",
            default_confidence=cmap.get(confidence_raw, 0.5),
        )
        if fig.revenue is None and fig.total_assets is None:
            continue  # too sparse to be useful
        if notes:
            fig.notes = notes[:400]
        out.append(fig)
    # Sort newest first.
    out.sort(key=lambda f: f.period_end or "", reverse=True)
    return out
