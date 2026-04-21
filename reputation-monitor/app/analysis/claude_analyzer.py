"""Claude-powered article analyzer.

A single prompt produces a structured JSON object with everything the
scoring layer and UI need: sentiment, reputational risk, investment risk,
AML red flags, summary, key facts and category tags.

Falls back to a deterministic lexicon-only analysis if the Anthropic API
is unavailable, so the system always returns a result.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.analysis.risk_lexicon import RISK_CATEGORIES, category_weight, quick_keyword_hints
from app.config import get_settings
from app.llm import llm_available, llm_complete

logger = logging.getLogger(__name__)


RISK_LEVELS = ("none", "low", "medium", "high", "critical")
SEV_FROM_LEVEL = {"none": 0.5, "low": 2.5, "medium": 5.0, "high": 7.5, "critical": 9.5}
INVESTMENT_IMPACTS = ("positive", "neutral", "negative")


@dataclass
class Analysis:
    mentions_company: bool = True
    sentiment_score: float = 0.0
    sentiment_label: str = "neutral"
    risk_level: str = "none"
    risk_category: Optional[str] = None
    risk_categories: list[str] = field(default_factory=list)
    risk_keywords: list[str] = field(default_factory=list)
    severity: float = 0.0
    investment_impact: str = "neutral"
    investment_risk: float = 0.0
    # Credibility / fake-news assessment
    credibility_score: float = 0.7  # 0..1, 1 = highly credible
    is_likely_fake: bool = False
    credibility_notes: str = ""
    # Narrative
    summary: str = ""
    key_facts: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    positive_points: list[str] = field(default_factory=list)
    raw_llm_response: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mentions_company": self.mentions_company,
            "sentiment_score": self.sentiment_score,
            "sentiment_label": self.sentiment_label,
            "risk_level": self.risk_level,
            "risk_category": self.risk_category,
            "risk_categories": self.risk_categories,
            "risk_keywords": self.risk_keywords,
            "severity": self.severity,
            "investment_impact": self.investment_impact,
            "investment_risk": self.investment_risk,
            "credibility_score": self.credibility_score,
            "is_likely_fake": self.is_likely_fake,
            "credibility_notes": self.credibility_notes,
            "summary": self.summary,
            "key_facts": self.key_facts,
            "red_flags": self.red_flags,
            "positive_points": self.positive_points,
        }


_SYSTEM_PROMPT = """You are a senior AML / due-diligence analyst for a Polish financial institution. \
You analyse media articles about companies and return a strict JSON object \
suitable for downstream reputation scoring and investment-risk assessment.

Focus areas: corruption, bribery, fraud, money laundering, sanctions, management upheaval, \
regulatory fines (KNF/UOKiK/OFAC/CBA), legal proceedings, bankruptcy, ESG scandals. \
Consider Polish business context, common name variants and fleksja.

MENTIONS_COMPANY — be INCLUSIVE, not conservative. Set mentions_company=true whenever \
the article is clearly about the target company OR any of its aliases, products, brands, \
subsidiaries, holding, CEO/board or a story where the company is a named major actor. \
Set mentions_company=false ONLY when the article is about a completely unrelated entity \
that coincidentally shares a word/token with the company name. When in doubt → true. \
If the company is mentioned only as peripheral context but the article still carries \
risk-relevant signals about it → true with risk_level scaled accordingly.

CRITICAL — FAKE NEWS / CREDIBILITY: before accepting claims at face value you MUST assess \
the credibility of the article. Red flags for low credibility include: \
sensational/clickbait tone, unnamed sources only, quotes without attribution, \
obviously manipulated statistics, anonymous blog / low-reputation domain, \
contradictory to reputable reporting, political propaganda signals, \
AI-generated stylistic markers, cross-posting from rumor sites. \
Penalise credibility when these appear — a dubious article must not drive \
the company's risk score. Summarise your credibility judgment in credibility_notes.

You MUST return ONLY a JSON object — no prose, no markdown fences."""


_JSON_SCHEMA_HINT = """{
  "mentions_company": true | false,
  "sentiment_score": -1.0 .. 1.0,
  "sentiment_label": "very_negative" | "negative" | "neutral" | "positive" | "very_positive",
  "risk_level": "none" | "low" | "medium" | "high" | "critical",
  "risk_categories": [ "corruption" | "legal" | "management" | "sanctions" | "financial" | "money_laundering" | "regulatory" | "operational" | "esg" ],
  "risk_keywords": [ "short verbatim spans from the article that triggered risk", ... up to 12 ],
  "severity": 0.0 .. 10.0,
  "investment_impact": "positive" | "neutral" | "negative",
  "investment_risk": 0.0 .. 10.0,
  "credibility_score": 0.0 .. 1.0,    // 0 = likely fake/disinformation, 1 = tier-1 reputable reporting
  "is_likely_fake": true | false,
  "credibility_notes": "1 short Polish sentence explaining the credibility verdict",
  "summary": "2-3 Polish sentences describing what happened to the company.",
  "key_facts": [ "up to 5 concise Polish bullet points" ],
  "red_flags": [ "up to 5 AML/compliance red flags in Polish; empty if none" ],
  "positive_points": [ "up to 4 positive / encouraging signals for investors in Polish; empty if none" ]
}"""


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        # drop optional language tag
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1 :]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


def _coerce(data: dict, *, fallback_keywords: list[str]) -> Analysis:
    def _clip(v: Any, lo: float, hi: float, default: float = 0.0) -> float:
        try:
            return max(lo, min(hi, float(v)))
        except Exception:
            return default

    valid_cats = set(RISK_CATEGORIES.keys())
    cats_raw = data.get("risk_categories") or []
    if isinstance(cats_raw, str):
        cats_raw = [cats_raw]
    cats = [str(c).lower().strip() for c in cats_raw if c]
    cats = [c for c in cats if c in valid_cats]

    primary = None
    if cats:
        primary = max(cats, key=category_weight)

    level = str(data.get("risk_level") or "none").lower()
    if level not in RISK_LEVELS:
        level = "none"

    impact = str(data.get("investment_impact") or "neutral").lower()
    if impact not in INVESTMENT_IMPACTS:
        impact = "neutral"

    kws_raw = data.get("risk_keywords") or []
    if isinstance(kws_raw, str):
        kws_raw = [kws_raw]
    kws = [str(k).strip() for k in kws_raw if k][:12]
    if not kws and fallback_keywords:
        kws = fallback_keywords[:12]

    facts_raw = data.get("key_facts") or []
    if isinstance(facts_raw, str):
        facts_raw = [facts_raw]
    facts = [str(f).strip() for f in facts_raw if f][:6]

    flags_raw = data.get("red_flags") or []
    if isinstance(flags_raw, str):
        flags_raw = [flags_raw]
    flags = [str(f).strip() for f in flags_raw if f][:6]

    sev = _clip(data.get("severity"), 0.0, 10.0, SEV_FROM_LEVEL.get(level, 0.0))
    inv_risk = _clip(data.get("investment_risk"), 0.0, 10.0, sev * 0.9 if impact == "negative" else sev * 0.5)

    credibility = _clip(data.get("credibility_score"), 0.0, 1.0, 0.7)
    is_fake = bool(data.get("is_likely_fake")) or credibility < 0.35

    pos_raw = data.get("positive_points") or []
    if isinstance(pos_raw, str):
        pos_raw = [pos_raw]
    positive_points = [str(p).strip() for p in pos_raw if p][:5]

    sent = _clip(data.get("sentiment_score"), -1.0, 1.0, 0.0)
    sent_label = str(data.get("sentiment_label") or "").lower().strip()
    if sent_label not in {"very_negative", "negative", "neutral", "positive", "very_positive"}:
        if sent <= -0.6:
            sent_label = "very_negative"
        elif sent <= -0.2:
            sent_label = "negative"
        elif sent < 0.2:
            sent_label = "neutral"
        elif sent < 0.6:
            sent_label = "positive"
        else:
            sent_label = "very_positive"

    return Analysis(
        mentions_company=bool(data.get("mentions_company", True)),
        sentiment_score=sent,
        sentiment_label=sent_label,
        risk_level=level,
        risk_category=primary,
        risk_categories=cats,
        risk_keywords=kws,
        severity=sev,
        investment_impact=impact,
        investment_risk=inv_risk,
        credibility_score=credibility,
        is_likely_fake=is_fake,
        credibility_notes=str(data.get("credibility_notes") or "").strip()[:400],
        summary=str(data.get("summary") or "").strip()[:1200],
        key_facts=facts,
        red_flags=flags,
        positive_points=positive_points,
    )


def _fallback_analysis(text: str, fallback_keywords: list[str]) -> Analysis:
    """Deterministic result used when the LLM is unavailable."""
    cats: list[str] = []
    for cat, spec in RISK_CATEGORIES.items():
        for kw in spec["keywords"]:
            if kw.lower() in text.lower() and cat not in cats:
                cats.append(cat)
                break
    if not cats:
        return Analysis(
            summary="(Analiza offline — brak wyraźnych sygnałów ryzyka.)",
            credibility_score=0.6,
            credibility_notes="(Analiza offline — wiarygodność nieznana.)",
        )
    primary = max(cats, key=category_weight)
    weight = category_weight(primary)
    level = "high" if weight >= 9 else "medium" if weight >= 6 else "low"
    sev = SEV_FROM_LEVEL[level]
    return Analysis(
        sentiment_score=-0.4,
        sentiment_label="negative",
        risk_level=level,
        risk_category=primary,
        risk_categories=cats,
        risk_keywords=fallback_keywords[:10],
        severity=sev,
        investment_impact="negative",
        investment_risk=sev * 0.9,
        credibility_score=0.6,
        credibility_notes="(Analiza offline — wiarygodność nieznana.)",
        summary="(Analiza offline — wykryto słowa kluczowe ryzyka.)",
    )


def analyze_article_with_claude(
    *,
    company_name: str,
    aliases: list[str] | None,
    title: str | None,
    content: str | None,
    source: str | None = None,
    published_at: str | None = None,
) -> Analysis:
    """Main entry point. Returns an Analysis dataclass."""
    settings = get_settings()
    text_parts = []
    if title:
        text_parts.append(f"TYTUŁ: {title}")
    if source:
        text_parts.append(f"ŹRÓDŁO: {source}")
    if published_at:
        text_parts.append(f"DATA PUBLIKACJI: {published_at}")
    if content:
        # max_article_chars == 0 means "no truncation" — pass the full body.
        limit = settings.max_article_chars or 0
        text_parts.append(content if limit <= 0 else content[:limit])
    article_blob = "\n\n".join(text_parts).strip()

    if not article_blob:
        return Analysis(mentions_company=False, summary="(Brak treści artykułu.)")

    hint_keywords = quick_keyword_hints(article_blob)

    if not llm_available():
        logger.info("LLM key missing — using offline fallback analysis.")
        return _fallback_analysis(article_blob, hint_keywords)

    user_prompt = (
        f"SPÓŁKA: {company_name}\n"
        f"ALIASY: {', '.join(aliases or []) or '(brak)'}\n"
        f"SŁOWA-KLUCZOWE WYKRYTE LOKALNIE (hint, nie ufaj ślepo): "
        f"{', '.join(hint_keywords) or '(brak)'}\n\n"
        "Przeanalizuj poniższy artykuł i zwróć dokładnie jeden JSON zgodny ze schematem:\n"
        f"{_JSON_SCHEMA_HINT}\n\n"
        "WAŻNE:\n"
        "• Jeśli artykuł NIE dotyczy tej spółki → mentions_company=false, wszystkie ryzyka 0, credibility_score=0.5.\n"
        "• FAKE NEWS: uwzględnij domenę źródła, ton, anonimowość źródeł, brak faktów, clickbait.\n"
        "  Jeśli credibility_score < 0.35 ustaw is_likely_fake=true i severity/investment_risk zredukuj o połowę.\n"
        "• Renomowane domeny (pb.pl, bankier.pl, wyborcza.biz, rp.pl, forsal.pl, reuters, bloomberg,\n"
        "  ft.com, wsj.com) → credibility ≥ 0.8 chyba że ton bardzo emocjonalny.\n"
        "• Pisz summary, key_facts, red_flags, positive_points, credibility_notes PO POLSKU.\n"
        "• investment_risk oceniaj z perspektywy inwestora / partnera B2B.\n"
        "• positive_points: konkretne pozytywy (dobre wyniki, kontrakty, ekspansja, nagrody, ESG).\n"
        "• risk_keywords to krótkie dosłowne fragmenty z tekstu (nie kategorie).\n\n"
        f"ARTYKUŁ:\n{article_blob}"
    )
    raw = llm_complete(
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=settings.llm_max_tokens,
        purpose="article_analysis",
    )
    if not raw:
        return _fallback_analysis(article_blob, hint_keywords)

    data = _extract_json(raw)
    if not data:
        logger.warning("LLM returned non-JSON; falling back. Raw: %s", raw[:400])
        res = _fallback_analysis(article_blob, hint_keywords)
        res.raw_llm_response = raw
        return res
    result = _coerce(data, fallback_keywords=hint_keywords)
    result.raw_llm_response = json.dumps(data, ensure_ascii=False)
    return result


# ───────────────────────────────────────────────────────────────────────
# Company-level synthesis (SWOT / pros-cons / investment thesis)
# ───────────────────────────────────────────────────────────────────────

@dataclass
class CompanyInsights:
    ai_summary: str = ""
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    opportunities: list[str] = field(default_factory=list)
    threats: list[str] = field(default_factory=list)
    investment_thesis: str = ""


_SYNTH_SYSTEM = """You are a senior investment analyst summarising media intelligence \
about a company for an investor / AML due-diligence team. \
You receive a list of already-analysed articles (each with sentiment, risk, \
credibility and summary). Your job is to synthesise them into a compact \
SWOT report + investment thesis in Polish. \

Rules:
• Down-weight or IGNORE articles whose credibility_score is below 0.4 — treat \
  them as unverified rumours. Never use them as evidence for threats.
• Distinguish between FACTS (reputable sources, multiple corroborations) and \
  ALLEGATIONS (single source, low credibility) — only facts belong in threats.
• Be balanced: surface real positives as well as negatives.
• Concise bullets (max ~12 Polish words each).
• Return ONLY JSON — no prose, no markdown fences."""


_SYNTH_SCHEMA = """{
  "ai_summary": "4-6 zdań po polsku — zwięzłe streszczenie obecnej sytuacji spółki",
  "strengths": [ "3-6 punktów — mocne strony spółki na podstawie artykułów" ],
  "weaknesses": [ "3-6 punktów — słabe strony / wewnętrzne ryzyka" ],
  "opportunities": [ "2-5 punktów — szanse i pozytywne trendy" ],
  "threats": [ "2-6 punktów — realne zewnętrzne zagrożenia (tylko z wiarygodnych źródeł)" ],
  "investment_thesis": "2-4 zdania po polsku: czy warto inwestować i dlaczego, zbalansowane"
}"""


def synthesize_company_insights(
    *,
    company_name: str,
    sector: Optional[str],
    analyses: list[dict[str, Any]],
) -> CompanyInsights:
    """Given per-article Analysis dicts, produce a company-level SWOT."""
    settings = get_settings()
    if not analyses:
        return CompanyInsights(
            ai_summary=f"Brak wystarczających danych o spółce {company_name}. Uruchom skan, aby rozpocząć analizę.",
        )

    # Trim down each analysis to what the synthesiser really needs
    slim = []
    for a in analyses[:40]:  # safety cap
        slim.append(
            {
                "title": (a.get("title") or "")[:200],
                "source": a.get("source"),
                "published_at": a.get("published_at"),
                "sentiment": a.get("sentiment_score"),
                "risk_level": a.get("risk_level"),
                "risk_categories": a.get("risk_categories"),
                "severity": a.get("severity"),
                "credibility": a.get("credibility_score"),
                "is_likely_fake": a.get("is_likely_fake"),
                "investment_impact": a.get("investment_impact"),
                "summary": (a.get("summary") or "")[:400],
                "key_facts": a.get("key_facts") or [],
                "red_flags": a.get("red_flags") or [],
                "positive_points": a.get("positive_points") or [],
            }
        )

    if not llm_available():
        return _fallback_insights(company_name, slim)

    prompt = (
        f"SPÓŁKA: {company_name}\n"
        f"SEKTOR: {sector or '(nieznany)'}\n\n"
        f"Liczba artykułów do syntezy: {len(slim)}\n\n"
        "DANE WEJŚCIOWE (JSON — wyniki analizy poszczególnych artykułów, w tym ocena wiarygodności):\n"
        f"{json.dumps(slim, ensure_ascii=False)[:18000]}\n\n"
        "Zwróć JSON zgodny ze schematem:\n"
        f"{_SYNTH_SCHEMA}\n"
    )
    raw = llm_complete(
        system=_SYNTH_SYSTEM,
        user=prompt,
        max_tokens=settings.llm_max_tokens,
        purpose="insights_synthesis",
    )
    if not raw:
        return _fallback_insights(company_name, slim)
    data = _extract_json(raw)
    if not data:
        logger.warning("Synth: non-JSON result, using fallback. Raw: %s", raw[:400])
        return _fallback_insights(company_name, slim)
    return _coerce_insights(data)


def _coerce_insights(data: dict) -> CompanyInsights:
    def _list(key: str, limit: int) -> list[str]:
        raw = data.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        return [str(x).strip() for x in raw if x][:limit]

    return CompanyInsights(
        ai_summary=str(data.get("ai_summary") or "").strip()[:1800],
        strengths=_list("strengths", 8),
        weaknesses=_list("weaknesses", 8),
        opportunities=_list("opportunities", 6),
        threats=_list("threats", 8),
        investment_thesis=str(data.get("investment_thesis") or "").strip()[:1200],
    )


def _fallback_insights(company_name: str, slim: list[dict[str, Any]]) -> CompanyInsights:
    strengths: list[str] = []
    weaknesses: list[str] = []
    threats: list[str] = []
    opportunities: list[str] = []
    for a in slim:
        cred = float(a.get("credibility") or 0.5)
        for p in a.get("positive_points") or []:
            if p and p not in strengths:
                strengths.append(p)
        if cred >= 0.5:
            for f in a.get("red_flags") or []:
                if f and f not in threats:
                    threats.append(f)
        if (a.get("investment_impact") == "negative") and cred >= 0.5:
            fact = (a.get("summary") or "").split(".")[0][:120]
            if fact and fact not in weaknesses:
                weaknesses.append(fact)

    summary = (
        f"Zebrano {len(slim)} artykułów o {company_name}. "
        f"Wysokiej wiarygodności: {sum(1 for a in slim if (a.get('credibility') or 0) >= 0.7)}. "
        "Pełna synteza wymaga klucza Claude."
    )
    return CompanyInsights(
        ai_summary=summary,
        strengths=strengths[:6],
        weaknesses=weaknesses[:6],
        opportunities=opportunities[:4],
        threats=threats[:6],
        investment_thesis="Synteza offline — wymagany klucz Claude dla pełnej tezy inwestycyjnej.",
    )


# ───────────────────────────────────────────────────────────────────────
# Company identity resolver — short input ("inpost") → full ID kit
# ───────────────────────────────────────────────────────────────────────

@dataclass
class ResolvedIdentity:
    canonical_name: str = ""
    nip: Optional[str] = None
    krs: Optional[str] = None
    sector: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    confidence: str = "low"  # low | medium | high
    notes: str = ""


_IDENTITY_SYSTEM = """You are a corporate-identity resolver for a Polish due-diligence tool.
Given a SHORT or INFORMAL query that refers to a company (e.g. "inpost",
"orlen", "cd projekt", "pko bp", "eurocash", "zondacrypto"), return STRICT
JSON with the canonical Polish company that the user most likely means.

Output JSON schema (no prose, no markdown):
{
  "canonical_name": "Official legal name as in KRS / MF white-list, e.g. 'InPost S.A.'",
  "nip": "10 digits without dashes, or null if unsure",
  "krs": "10 digits (with leading zeros) or null if unsure",
  "sector": "short sector label in Polish, e.g. 'logistyka / kurier'",
  "aliases": ["common short/brand/international variants used in Polish media",
              "e.g. for InPost: InPost, Grupa InPost, InPost Paczkomaty, InPost S.A."],
  "confidence": "low | medium | high",
  "notes": "1-sentence reason"
}

Rules:
- If the query is clearly a well-known Polish public/private company → confidence "high"
  and you MUST provide canonical_name + aliases (at least 3 when possible).
- If there are multiple candidates → pick the most prominent / largest in Polish media
  and note ambiguity in "notes".
- If you cannot identify the company (too generic, gibberish) → confidence "low"
  and canonical_name = "" (empty).
- NEVER invent a NIP / KRS you are not sure about — set them to null if unsure.
- canonical_name must be the real legal name (with "S.A.", "Sp. z o.o." etc.).
- aliases: include the bare brand, the full legal form, any common international name,
  and any parent group if widely used in media. Do NOT include generic words like
  "firma", "spółka", "grupa" on their own.
"""


def resolve_company_identity(query: str) -> Optional[ResolvedIdentity]:
    """Use Claude to map a short/informal input to a canonical Polish company.

    Returns None when there is no API key, the call fails, or Claude could not
    identify a company with any confidence.
    """
    q = (query or "").strip()
    if not q:
        return None

    if not llm_available():
        return None

    raw = llm_complete(
        system=_IDENTITY_SYSTEM,
        user=f"QUERY: {q}",
        max_tokens=600,
        purpose="identity_resolver",
    )
    if not raw:
        return None
    data = _extract_json(raw)
    if not data:
        logger.info("Identity resolver — non-JSON reply for %r: %s", q, raw[:200])
        return None

    canonical = (data.get("canonical_name") or "").strip()
    confidence = (data.get("confidence") or "low").strip().lower()
    if not canonical or confidence == "low":
        return None

    # Normalise NIP / KRS (digits only).
    nip_raw = (data.get("nip") or "")
    krs_raw = (data.get("krs") or "")
    nip_digits = "".join(ch for ch in str(nip_raw) if ch.isdigit())
    krs_digits = "".join(ch for ch in str(krs_raw) if ch.isdigit())
    nip = nip_digits if len(nip_digits) == 10 else None
    krs = krs_digits.zfill(10) if 1 <= len(krs_digits) <= 10 else None

    aliases_raw = data.get("aliases") or []
    aliases: list[str] = []
    for a in aliases_raw if isinstance(aliases_raw, list) else []:
        a = str(a or "").strip()
        if len(a) >= 2 and a.lower() != canonical.lower() and a not in aliases:
            aliases.append(a)
    # Always include the bare user query as an alias — that is what the media
    # often uses (e.g. "InPost" without ".S.A.").
    if q and q.lower() not in (canonical.lower(), *[a.lower() for a in aliases]):
        aliases.append(q)

    return ResolvedIdentity(
        canonical_name=canonical,
        nip=nip,
        krs=krs,
        sector=(data.get("sector") or "").strip()[:128] or None,
        aliases=aliases[:8],
        confidence=confidence if confidence in ("low", "medium", "high") else "medium",
        notes=(data.get("notes") or "").strip()[:240],
    )
