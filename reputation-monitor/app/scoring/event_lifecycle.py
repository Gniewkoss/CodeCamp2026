"""Time-aware contribution of risk events to the company ledger score.

Reproducible: same events + same `as_of` (timezone-aware UTC) → same outputs.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RiskEvent


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _event_anchor(ev: RiskEvent) -> datetime:
    d = _as_utc(ev.event_date) or _as_utc(ev.detected_at) or _as_utc(ev.created_at)
    return d or datetime.now(timezone.utc)


def get_event_risk_contribution(event: RiskEvent, as_of: datetime) -> float:
    """Risk contribution 0..~1 after decay rules (spec)."""
    if getattr(event, "is_excluded", False):
        return 0.0
    as_of = _as_utc(as_of) or datetime.now(timezone.utc)
    base_severity = float(event.severity or 0.0)
    anchor = _event_anchor(event)
    age_days = max(0, (as_of - anchor).days)

    st = (event.status or "active").lower()

    if st == "active":
        decay = 1.0 if age_days <= 30 else math.exp(-0.01155 * (age_days - 30))
        return base_severity * decay

    if st in ("resolved", "mitigated"):
        res = _as_utc(event.resolved_at)
        if res is None:
            return base_severity * math.exp(-0.01155 * max(0, age_days - 30))
        days_since_resolution = max(0, (as_of - res).days)
        post_resolution_decay = math.exp(-0.0231 * days_since_resolution)
        residual = max(0.15, post_resolution_decay)
        return base_severity * residual

    if st == "historical":
        decay = math.exp(-0.003 * age_days)
        return base_severity * max(0.10, decay)

    return base_severity


def calculate_company_score(db: Session, company_id: str, as_of: datetime | None = None) -> dict[str, Any]:
    """Aggregate ledger score 0–100 from risk events (spec)."""
    as_of = _as_utc(as_of) or datetime.now(timezone.utc)
    events = list(
        db.scalars(
            select(RiskEvent)
            .where(RiskEvent.company_id == company_id)
            .order_by(RiskEvent.detected_at.desc())
        ).all()
    )
    visible = [e for e in events if not getattr(e, "is_excluded", False)]

    event_contributions: list[dict[str, Any]] = []
    for event in visible:
        contrib = get_event_risk_contribution(event, as_of)
        event_contributions.append(
            {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "title": event.title,
                "contribution": round(contrib, 6),
                "status": event.status,
                "related_person": event.related_person,
            }
        )

    raw_score = sum(e["contribution"] for e in event_contributions)
    sanctions_hits = [
        e for e in visible if e.event_type and "sanction" in e.event_type
    ]
    normalized = min(100.0, raw_score * 20.0)
    # Sanctions floor on the 0–100 scale (spec intent: always high risk if listed).
    if sanctions_hits:
        normalized = max(normalized, 80.0)

    return {
        "score": round(normalized, 1),
        "raw_ledger_sum": round(raw_score, 6),
        "event_count": len(visible),
        "active_events": len([e for e in visible if (e.status or "").lower() == "active"]),
        "sanctions_hits": len(sanctions_hits),
        "breakdown": event_contributions,
        "as_of": as_of.isoformat(),
    }
