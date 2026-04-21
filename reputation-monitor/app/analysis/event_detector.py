"""Claude-powered extraction of structured risk events from news articles."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.event_types import EVENT_TYPES, EVENT_TYPE_CHOICES
from app.config import get_settings
from app.models import Article, ArticleAnalysis, Company, RiskEvent

logger = logging.getLogger(__name__)

_SYSTEM = """You are a compliance analyst. Extract concrete risk EVENTS from a news article.
Return ONLY a JSON object with an "events" array (no markdown), shaped like:
{
  "events": [
    {
      "event_type": string (must be one of the allowed types),
      "title": string max 80 chars, Polish,
      "description": string, Polish, factual,
      "event_date": "YYYY-MM-DD" or null,
      "related_person": string full name or null,
      "severity_modifier": number 0.5-1.5
    }
  ]
}
If there is no strong evidence of a distinct event, return {"events": []}."""


def _extract_json_array(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    # Prefer an object with "events": [...] (new schema)
    m_obj = re.search(r"\{[\s\S]*\}", raw)
    if m_obj:
        try:
            obj = json.loads(m_obj.group())
            if isinstance(obj, dict):
                evs = obj.get("events")
                if isinstance(evs, list):
                    return evs
        except json.JSONDecodeError:
            pass
    # Fallback: bare array (legacy prompt)
    m_arr = re.search(r"\[[\s\S]*\]", raw)
    if not m_arr:
        return []
    try:
        data = json.loads(m_arr.group())
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _parse_event_date(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()[:10]
    try:
        return datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), tzinfo=timezone.utc)
    except Exception:
        return None


def detect_events_from_article(db: Session, article: Article, analysis: ArticleAnalysis, company: Company) -> List[RiskEvent]:
    settings = get_settings()
    text = (article.content or "")[:3000]
    if not text.strip():
        return []
    # Do not extract RiskEvents from articles that don't concern the company —
    # Claude otherwise happily invents events about whoever the article IS
    # about (e.g. Zondacrypto) and we attach them to the wrong company.
    if analysis.mentions_company is False:
        return []
    allowed = ", ".join(EVENT_TYPE_CHOICES)
    user = (
        f"SPÓŁKA: {company.name}\n"
        f"Dozwolone event_type (wybierz dokładnie jeden z listy): {allowed}\n\n"
        f"Artykuł:\n{text}\n"
    )
    from app.llm import llm_available, llm_complete

    out: list[RiskEvent] = []
    if not llm_available():
        return out
    raw = llm_complete(
        system=_SYSTEM,
        user=user,
        max_tokens=min(settings.llm_max_tokens, 2000),
        purpose="event_detector",
    )
    if not raw:
        return out
    items = _extract_json_array(raw)

    now = datetime.now(timezone.utc)
    for it in items:
        et = str(it.get("event_type") or "").strip()
        if et not in EVENT_TYPES:
            continue
        base = float(EVENT_TYPES[et])
        mod = float(it.get("severity_modifier") or 1.0)
        mod = max(0.5, min(1.5, mod))
        sev = max(0.0, min(1.0, base * mod))
        title = (it.get("title") or et)[:512]
        # Dedupe: same article + title + type
        existing = db.scalar(
            select(RiskEvent)
            .where(
                RiskEvent.company_id == company.id,
                RiskEvent.article_id == article.id,
                RiskEvent.event_type == et,
                RiskEvent.title == title,
            )
            .limit(1)
        )
        if existing:
            continue
        ev = RiskEvent(
            company_id=company.id,
            article_id=article.id,
            event_type=et,
            title=title,
            description=(it.get("description") or "")[:8000] or None,
            severity=sev,
            source_url=article.url,
            source_name=article.source,
            detected_at=now,
            event_date=_parse_event_date(it.get("event_date")),
            status="active",
            related_person=(it.get("related_person") or None),
        )
        db.add(ev)
        out.append(ev)
    if out:
        db.commit()
        for e in out:
            db.refresh(e)
    return out
