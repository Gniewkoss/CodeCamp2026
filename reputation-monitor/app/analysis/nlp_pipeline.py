from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rapidfuzz import fuzz, process

from app.analysis.risk_lexicon import dominant_category, match_risk_keywords
from app.analysis.sentiment import sentiment_for_text
from app.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import Article


@dataclass
class AnalysisResult:
    mentions_company: bool
    sentiment_score: float
    risk_keywords: list[str]
    risk_category: str | None
    severity: float
    raw_llm_response: str | None


_nlp_pl = None


def _get_spacy_pl():
    global _nlp_pl
    if _nlp_pl is False:
        return None
    if _nlp_pl is not None:
        return _nlp_pl
    try:
        import spacy

        _nlp_pl = spacy.load("pl_core_news_lg")
        return _nlp_pl
    except Exception:
        _nlp_pl = False
        return None


def _normalize_entity(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def article_mentions_company(text: str, company_name: str, aliases: list[str] | None) -> bool:
    if not text:
        return False
    names = [_normalize_entity(company_name)]
    if aliases:
        names.extend(_normalize_entity(a) for a in aliases if a)
    names = [n for n in names if len(n) >= 2]
    blob = _normalize_entity(text[:200_000])
    for n in names:
        if len(n) >= 3 and n in blob:
            return True
        if len(n) >= 3 and fuzz.partial_ratio(n, blob) >= 88:
            return True
    nlp = _get_spacy_pl()
    if nlp:
        doc = nlp(text[:50_000])
        ents = {_normalize_entity(e.text) for e in doc.ents if e.label_ in ("ORG", "PRODUCT", "MISC", "PER")}
        choices = list({*names, *[e for e in ents if len(e) > 2]})
        for ent in ents:
            match = process.extractOne(ent, choices, scorer=fuzz.token_sort_ratio)
            if match and match[1] >= 82:
                for n in names:
                    if fuzz.token_sort_ratio(ent, n) >= 78 or n in ent or ent in n:
                        return True
    return False


def _severity_from_matches(keyword_strings: list[str], sentiment: float) -> float:
    base = min(10.0, len(keyword_strings) * 1.5)
    if sentiment < 0:
        base += abs(sentiment) * 3
    return max(0.0, min(10.0, base))


def _claude_analyze(article_text: str) -> dict | None:
    settings = get_settings()
    key = settings.anthropic_api_key
    if not key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        prompt = (
            "Analyze this news article about a company for AML/due diligence risk signals.\n"
            'Return JSON only: {"risk_level": "high|medium|low|none", "risk_categories": [...], '
            '"summary": "...", "key_facts": [...], "sentiment": -1.0 to 1.0}\n'
            f"Article:\n{article_text[:12000]}"
        )
        msg = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in msg.content:
            if hasattr(block, "text"):
                text += block.text
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
    except Exception:
        return None
    return None


def run_analysis(
    article: "Article",
    company_name: str,
    company_aliases: list[str] | None,
    *,
    use_llm: bool = True,
) -> AnalysisResult:
    text_parts = [article.title or "", article.content or ""]
    full = "\n\n".join(t for t in text_parts if t)
    mentions = article_mentions_company(full, company_name, company_aliases)
    lang = (article.language or "pl").lower()

    matches = match_risk_keywords(full)
    llm_json = _claude_analyze(full) if use_llm else None
    if llm_json:
        cats = [str(c).lower() for c in llm_json.get("risk_categories") or []]
        local_kws = [m.keyword for m in matches]
        llm_keys = [c for c in cats if c]
        merged_kw = list(dict.fromkeys(local_kws + llm_keys))[:64]
        sent = float(llm_json.get("sentiment") or 0.0)
        sent = max(-1.0, min(1.0, sent))
        sev_map = {"high": 8.0, "medium": 5.0, "low": 2.5, "none": 0.5}
        rl = str(llm_json.get("risk_level") or "none").lower()
        severity = sev_map.get(rl, 3.0)
        primary = dominant_category(matches) or (cats[0] if cats else None)
        if local_kws:
            severity = max(severity, _severity_from_matches(local_kws, sent))
        return AnalysisResult(
            mentions_company=mentions,
            sentiment_score=sent,
            risk_keywords=merged_kw,
            risk_category=primary,
            severity=severity,
            raw_llm_response=json.dumps(llm_json, ensure_ascii=False),
        )

    kws = [m.keyword for m in matches]
    primary = dominant_category(matches)
    sentiment = sentiment_for_text(full, lang)
    severity = _severity_from_matches(kws, sentiment)
    return AnalysisResult(
        mentions_company=mentions,
        sentiment_score=sentiment,
        risk_keywords=kws[:64],
        risk_category=primary,
        severity=severity,
        raw_llm_response=None,
    )


def persist_analysis(db: "Session", article: "Article", result: AnalysisResult) -> uuid.UUID:
    from app.models import ArticleAnalysis

    existing = article.analysis
    if existing:
        existing.sentiment_score = result.sentiment_score
        existing.risk_keywords = result.risk_keywords
        existing.risk_category = result.risk_category
        existing.severity = result.severity
        existing.raw_llm_response = result.raw_llm_response
        existing.analyzed_at = datetime.now(timezone.utc)
        db.add(existing)
        db.flush()
        return existing.id

    row = ArticleAnalysis(
        article_id=article.id,
        sentiment_score=result.sentiment_score,
        risk_keywords=result.risk_keywords,
        risk_category=result.risk_category,
        severity=result.severity,
        raw_llm_response=result.raw_llm_response,
    )
    db.add(row)
    db.flush()
    return row.id
