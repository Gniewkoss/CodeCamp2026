"""Create / update risk_events from sanctions screening."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.event_types import EVENT_TYPES
from app.analysis.sanctions_checker import SanctionsMatch, check_names
from app.models import Company, CompanyPerson, RiskEvent

logger = logging.getLogger(__name__)


def apply_sanctions_check(db: Session, company_id: str) -> List[RiskEvent]:
    company = db.get(Company, company_id)
    if not company:
        return []
    persons = list(
        db.scalars(
            select(CompanyPerson).where(
                CompanyPerson.company_id == company_id,
                CompanyPerson.is_active.is_(True),
            )
        ).all()
    )
    cnames = [company.name] + list(company.aliases or [])
    pnames = [p.full_name for p in persons]
    try:
        matches = check_names(company_names=cnames, person_names=pnames)
    except Exception as e:
        logger.warning("Sanctions check failed: %s", e)
        return []

    created: list[RiskEvent] = []
    now = datetime.now(timezone.utc)
    for m in matches:
        et = "sanctions_match_company" if m.match_type == "company" else "sanctions_match_person"
        title = f"Sankcje: trafienie ({m.match_type}) — {m.matched_entity[:80]}"
        dup = db.scalar(
            select(RiskEvent).where(
                RiskEvent.company_id == company_id,
                RiskEvent.event_type == et,
                RiskEvent.title == title,
                RiskEvent.status == "active",
            )
        )
        if dup:
            continue
        ev = RiskEvent(
            company_id=company_id,
            event_type=et,
            title=title[:512],
            description=f"Dopasowanie fuzzy {m.match_score:.0f}% do wpisu: {m.matched_entity}",
            severity=float(EVENT_TYPES.get(et, 0.85)),
            source_url="https://www.gov.pl/web/mswia/lista-ostrzezen",
            source_name=m.list_name,
            detected_at=now,
            status="active",
            sanctions_list=m.list_name[:64],
            related_person=None if m.match_type == "company" else m.matched_entity[:512],
        )
        db.add(ev)
        created.append(ev)
    if created:
        db.commit()
        for e in created:
            db.refresh(e)
    return created
