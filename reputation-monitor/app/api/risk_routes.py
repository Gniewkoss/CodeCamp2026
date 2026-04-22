"""Risk ledger: events, registry refresh, sanctions, company-level ledger API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.event_types import EVENT_TYPES
from app.database import get_db
from app.models import Company, CompanyPerson, CompanyRegistryData, RiskEvent
from app.scoring.calculator import latest_score_for_company, recalculate_and_persist, score_history_series
from app.scoring.event_lifecycle import calculate_company_score, get_event_risk_contribution
from app.services.registry_sync import (
    sync_all_registries,
    sync_ceidg_v2_for_company,
    sync_gus_bir_for_company,
    sync_krs_for_company,
)
from app.services.sanctions_sync import apply_sanctions_check

router = APIRouter()


class RiskEventIn(BaseModel):
    event_type: str
    title: str
    description: Optional[str] = None
    severity: float = Field(ge=0.0, le=1.0, default=0.6)
    event_date: Optional[datetime] = None
    related_person: Optional[str] = None
    source_url: Optional[str] = None
    source_name: Optional[str] = None
    status: str = "active"


class RiskEventUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[float] = Field(None, ge=0.0, le=1.0)
    event_date: Optional[datetime] = None
    related_person: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_at: Optional[datetime] = None


class ResolveBody(BaseModel):
    resolution_note: str
    resolved_at: Optional[datetime] = None


class RiskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    event_type: str
    title: str
    description: Optional[str]
    severity: float
    source_url: Optional[str]
    source_name: Optional[str]
    detected_at: datetime
    event_date: Optional[datetime]
    status: str
    resolved_at: Optional[datetime]
    resolution_note: Optional[str]
    related_person: Optional[str]
    article_id: Optional[str]
    sanctions_list: Optional[str]
    is_excluded: bool
    created_at: datetime
    risk_contribution: Optional[float] = None


def _event_out(db: Session, e: RiskEvent) -> RiskEventOut:
    now = datetime.now(timezone.utc)
    contrib = get_event_risk_contribution(e, now) if not e.is_excluded else 0.0
    return RiskEventOut(
        id=e.id,
        company_id=e.company_id,
        event_type=e.event_type,
        title=e.title,
        description=e.description,
        severity=float(e.severity),
        source_url=e.source_url,
        source_name=e.source_name,
        detected_at=e.detected_at,
        event_date=e.event_date,
        status=e.status,
        resolved_at=e.resolved_at,
        resolution_note=e.resolution_note,
        related_person=e.related_person,
        article_id=e.article_id,
        sanctions_list=e.sanctions_list,
        is_excluded=bool(e.is_excluded),
        created_at=e.created_at,
        risk_contribution=round(contrib * 20.0, 2),
    )


@router.get("/api/companies/{company_id}/events", response_model=List[RiskEventOut])
def list_events(company_id: str, db: Session = Depends(get_db)) -> List[RiskEventOut]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    rows = list(
        db.scalars(
            select(RiskEvent).where(RiskEvent.company_id == company_id).order_by(RiskEvent.detected_at.desc())
        ).all()
    )
    return [_event_out(db, e) for e in rows]


@router.get("/api/companies/{company_id}/events/{event_id}", response_model=RiskEventOut)
def get_event(company_id: str, event_id: str, db: Session = Depends(get_db)) -> RiskEventOut:
    e = db.get(RiskEvent, event_id)
    if not e or e.company_id != company_id:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_out(db, e)


@router.post("/api/companies/{company_id}/events", response_model=RiskEventOut, status_code=201)
def create_event(company_id: str, payload: RiskEventIn, db: Session = Depends(get_db)) -> RiskEventOut:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    if payload.event_type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown event_type: {payload.event_type}")
    ev = RiskEvent(
        company_id=company_id,
        event_type=payload.event_type,
        title=payload.title[:512],
        description=payload.description,
        severity=payload.severity,
        source_url=payload.source_url,
        source_name=payload.source_name,
        event_date=payload.event_date,
        related_person=payload.related_person,
        status=payload.status,
        detected_at=datetime.now(timezone.utc),
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    recalculate_and_persist(db, company_id, lookback_days=90)
    return _event_out(db, ev)


@router.put("/api/companies/{company_id}/events/{event_id}", response_model=RiskEventOut)
def update_event(
    company_id: str, event_id: str, payload: RiskEventUpdate, db: Session = Depends(get_db)
) -> RiskEventOut:
    e = db.get(RiskEvent, event_id)
    if not e or e.company_id != company_id:
        raise HTTPException(status_code=404, detail="Event not found")
    if payload.title is not None:
        e.title = payload.title[:512]
    if payload.description is not None:
        e.description = payload.description
    if payload.status is not None:
        e.status = payload.status
    if payload.severity is not None:
        e.severity = payload.severity
    if payload.event_date is not None:
        e.event_date = payload.event_date
    if payload.related_person is not None:
        e.related_person = payload.related_person
    if payload.resolution_note is not None:
        e.resolution_note = payload.resolution_note
    if payload.resolved_at is not None:
        e.resolved_at = payload.resolved_at
    db.commit()
    db.refresh(e)
    recalculate_and_persist(db, company_id, lookback_days=90)
    return _event_out(db, e)


@router.delete("/api/companies/{company_id}/events/{event_id}", status_code=204)
def exclude_event(company_id: str, event_id: str, db: Session = Depends(get_db)) -> None:
    e = db.get(RiskEvent, event_id)
    if not e or e.company_id != company_id:
        raise HTTPException(status_code=404, detail="Event not found")
    e.is_excluded = True
    e.status = "historical"
    e.resolution_note = (e.resolution_note or "") + "\n[Wykluczono z scoringu — false positive / API DELETE]"
    e.resolved_at = datetime.now(timezone.utc)
    db.commit()
    recalculate_and_persist(db, company_id, lookback_days=90)


@router.post("/api/companies/{company_id}/events/{event_id}/resolve", response_model=RiskEventOut)
def resolve_event(
    company_id: str, event_id: str, body: ResolveBody, db: Session = Depends(get_db)
) -> RiskEventOut:
    e = db.get(RiskEvent, event_id)
    if not e or e.company_id != company_id:
        raise HTTPException(status_code=404, detail="Event not found")
    e.status = "resolved"
    e.resolution_note = body.resolution_note
    e.resolved_at = body.resolved_at or datetime.now(timezone.utc)
    db.commit()
    db.refresh(e)
    recalculate_and_persist(db, company_id, lookback_days=90)
    return _event_out(db, e)


@router.post("/api/companies/{company_id}/registry/refresh")
def refresh_registry(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    return sync_all_registries(db, company_id)


@router.post("/api/companies/{company_id}/registry/krs")
def refresh_krs_only(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    return sync_krs_for_company(db, company_id)


@router.post("/api/companies/{company_id}/registry/ceidg")
def refresh_ceidg_only(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    return sync_ceidg_v2_for_company(db, company_id)


@router.post("/api/companies/{company_id}/registry/gus")
def refresh_gus_bir(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Pobierz dane z GUS BIR/REGON (produkcja) — PKD, forma, BIR11."""
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    return sync_gus_bir_for_company(db, company_id)


@router.post("/api/companies/{company_id}/sanctions/recheck")
def recheck_sanctions(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    new_ev = apply_sanctions_check(db, company_id)
    recalculate_and_persist(db, company_id, lookback_days=90)
    return {"new_matches": len(new_ev), "event_ids": [e.id for e in new_ev]}


@router.post("/api/admin/cleanup-orphan-events")
def cleanup_orphan_events(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """One-shot cleanup: delete RiskEvents whose source article was marked
    by Claude as NOT mentioning the company (``mentions_company=false``).

    Those events were created by an old version of the event-detector that
    ran regardless of relevance and polluted companies like mBank with
    Zondacrypto-derived "key concerns". Safe to call repeatedly.
    """
    from app.models import Article, ArticleAnalysis, RiskEvent

    bad_ids = list(
        db.scalars(
            select(RiskEvent.id)
            .join(Article, Article.id == RiskEvent.article_id)
            .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
            .where(ArticleAnalysis.mentions_company.is_(False))
        ).all()
    )
    if not bad_ids:
        return {"deleted": 0, "companies_rescored": 0}
    affected = list(
        db.scalars(
            select(RiskEvent.company_id).where(RiskEvent.id.in_(bad_ids)).distinct()
        ).all()
    )
    for cid in affected:
        db.execute(
            RiskEvent.__table__.delete().where(
                RiskEvent.company_id == cid, RiskEvent.id.in_(bad_ids)
            )
        )
    db.commit()
    for cid in affected:
        try:
            recalculate_and_persist(db, cid, lookback_days=90)
        except Exception:
            continue
    return {"deleted": len(bad_ids), "companies_rescored": len(affected)}


@router.get("/api/ledger/companies")
def ledger_company_list(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    companies = list(db.scalars(select(Company).order_by(Company.name)).all())
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for c in companies:
        ledger = calculate_company_score(db, c.id, as_of=now)
        latest = latest_score_for_company(db, c.id)
        sanctions_flag = ledger["sanctions_hits"] > 0
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "nip": c.nip,
                "krs": c.krs,
                "ledger_score": ledger["score"],
                "display_score": float(latest.score) if latest else ledger["score"],
                "active_events": ledger["active_events"],
                "event_count": ledger["event_count"],
                "sanctions_flag": sanctions_flag,
                "recommendation": latest.recommendation if latest else None,
                "last_snapshot_at": latest.timestamp.isoformat() if latest and latest.timestamp else None,
            }
        )
    out.sort(key=lambda x: x["ledger_score"], reverse=True)
    return out


@router.get("/api/companies/{company_id}/ledger")
def company_ledger_detail(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = db.get(Company, company_id)
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")
    now = datetime.now(timezone.utc)
    ledger = calculate_company_score(db, company_id, as_of=now)
    events = list(
        db.scalars(
            select(RiskEvent).where(RiskEvent.company_id == company_id).order_by(RiskEvent.detected_at.desc())
        ).all()
    )
    persons = list(db.scalars(select(CompanyPerson).where(CompanyPerson.company_id == company_id)).all())
    reg_rows = list(
        db.scalars(
            select(CompanyRegistryData)
            .where(CompanyRegistryData.company_id == company_id)
            .order_by(CompanyRegistryData.extracted_at.desc())
            .limit(15)
        ).all()
    )
    history = score_history_series(db, company_id, days=180)
    timeline = [
        {
            "t": h.timestamp.isoformat() if h.timestamp else None,
            "score": float(h.score),
            "investment_score": float(h.investment_score) if h.investment_score is not None else None,
            "ledger_score": float(h.ledger_score) if h.ledger_score is not None else None,
            "active_events": h.active_event_count,
            "sanctions_hits": h.sanctions_match_count,
        }
        for h in history
    ]
    return {
        "company": {
            "id": c.id,
            "name": c.name,
            "nip": c.nip,
            "krs": c.krs,
            "aliases": c.aliases,
        },
        "ledger": ledger,
        "events": [_event_out(db, e).model_dump() for e in events],
        "persons": [
            {
                "id": p.id,
                "full_name": p.full_name,
                "role": p.role,
                "start_date": p.start_date,
                "end_date": p.end_date,
                "is_active": p.is_active,
            }
            for p in persons
        ],
        "registry_snapshots": [
            {"id": r.id, "source": r.source, "extracted_at": r.extracted_at.isoformat(), "has_raw": bool(r.raw_json)}
            for r in reg_rows
        ],
        "score_timeline": timeline,
    }


@router.get("/api/companies/{company_id}/registry/data", response_model=List[Dict[str, Any]])
def list_registry_data(company_id: str, limit: int = 20, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    rows = list(
        db.scalars(
            select(CompanyRegistryData)
            .where(CompanyRegistryData.company_id == company_id)
            .order_by(CompanyRegistryData.extracted_at.desc())
            .limit(limit)
        ).all()
    )
    return [
        {
            "id": r.id,
            "source": r.source,
            "extracted_at": r.extracted_at.isoformat(),
            "raw_json": r.raw_json,
        }
        for r in rows
    ]
