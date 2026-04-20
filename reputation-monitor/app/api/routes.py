import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Article, ArticleAnalysis, Company, ScoreHistory
from app.scraper.tasks import scrape_company_sync
from app.scoring.calculator import (
    calculate_score,
    latest_score_for_company,
    score_history_series,
    top_risk_companies,
)

router = APIRouter()


class CompanyCreate(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)
    nip: Optional[str] = None
    krs: Optional[str] = None


class CompanyOut(BaseModel):
    id: uuid.UUID
    name: str
    aliases: Optional[List[str]]
    nip: Optional[str]
    krs: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class CompanyDetailOut(CompanyOut):
    current_score: Optional[float] = None
    score_components: Optional[Dict[str, Any]] = None


class ScorePoint(BaseModel):
    timestamp: datetime
    score: float
    score_components: Optional[Dict[str, Any]]
    article_count: int


class ArticleOut(BaseModel):
    id: uuid.UUID
    url: str
    title: Optional[str]
    source: Optional[str]
    published_at: Optional[datetime]
    language: Optional[str]
    sentiment_score: Optional[float]
    risk_keywords: Optional[List[str]]
    risk_category: Optional[str]
    severity: Optional[float]

    class Config:
        from_attributes = True


class TopRiskArticle(BaseModel):
    article_id: uuid.UUID
    company_id: uuid.UUID
    company_name: str
    title: Optional[str]
    source: Optional[str]
    published_at: Optional[datetime]
    severity: Optional[float]
    risk_category: Optional[str]


@router.get("/companies", response_model=List[CompanyOut])
def list_companies(db: Session = Depends(get_db)) -> List[Company]:
    return list(db.scalars(select(Company).order_by(Company.name)).all())


@router.post("/companies", response_model=CompanyOut)
def create_company(payload: CompanyCreate, db: Session = Depends(get_db)) -> Company:
    c = Company(
        name=payload.name.strip(),
        aliases=payload.aliases or None,
        nip=payload.nip.strip() if payload.nip else None,
        krs=payload.krs.strip() if payload.krs else None,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/companies/{company_id}", response_model=CompanyDetailOut)
def get_company(company_id: uuid.UUID, db: Session = Depends(get_db)) -> CompanyDetailOut:
    c = db.get(Company, company_id)
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")
    latest = latest_score_for_company(db, c.id)
    if latest is None:
        calculate_score(c.id, lookback_days=90, db=db, persist=True)
        latest = latest_score_for_company(db, c.id)
    return CompanyDetailOut(
        id=c.id,
        name=c.name,
        aliases=c.aliases,
        nip=c.nip,
        krs=c.krs,
        created_at=c.created_at,
        current_score=float(latest.score) if latest else None,
        score_components=latest.score_components if latest else None,
    )


@router.get("/companies/{company_id}/score/history", response_model=List[ScorePoint])
def score_history(company_id: uuid.UUID, days: int = 90, db: Session = Depends(get_db)) -> List[ScorePoint]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    rows = score_history_series(db, company_id, days=days)
    return [
        ScorePoint(
            timestamp=r.timestamp,
            score=float(r.score),
            score_components=r.score_components,
            article_count=r.article_count,
        )
        for r in rows
    ]


@router.get("/companies/{company_id}/articles", response_model=List[ArticleOut])
def company_articles(
    company_id: uuid.UUID,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> List[ArticleOut]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    stmt = (
        select(Article)
        .options(joinedload(Article.analysis))
        .where(Article.company_id == company_id)
        .order_by(Article.published_at.desc().nulls_last(), Article.scraped_at.desc())
        .limit(limit)
    )
    arts = db.execute(stmt).unique().scalars().all()
    out: List[ArticleOut] = []
    for a in arts:
        an = a.analysis
        out.append(
            ArticleOut(
                id=a.id,
                url=a.url,
                title=a.title,
                source=a.source,
                published_at=a.published_at,
                language=a.language,
                sentiment_score=float(an.sentiment_score) if an and an.sentiment_score is not None else None,
                risk_keywords=an.risk_keywords if an else None,
                risk_category=an.risk_category if an else None,
                severity=float(an.severity) if an and an.severity is not None else None,
            )
        )
    return out


@router.post("/companies/{company_id}/scan")
def trigger_scan(company_id: uuid.UUID, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    result = scrape_company_sync(db, company_id)
    return {"status": "ok", **result}


@router.get("/search", response_model=List[CompanyOut])
def search_companies(q: str = Query(..., min_length=1), db: Session = Depends(get_db)) -> List[Company]:
    needle = f"%{q.strip()}%"
    stmt = select(Company).where(
        or_(
            Company.name.ilike(needle),
            Company.nip.ilike(needle),
        )
    )
    direct = list(db.scalars(stmt).all())
    if direct:
        return direct
    all_c = list(db.scalars(select(Company)).all())
    ranked: List[Tuple[Company, int]] = []
    ql = q.strip().lower()
    for c in all_c:
        score = fuzz.partial_ratio(ql, c.name.lower())
        if c.aliases:
            score = max(score, max((fuzz.partial_ratio(ql, a.lower()) for a in c.aliases), default=0))
        if c.nip:
            score = max(score, fuzz.partial_ratio(ql, c.nip.lower()))
        if score >= 72:
            ranked.append((c, score))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in ranked[:25]]


@router.get("/dashboard/top-risks")
def dashboard_top_risks(db: Session = Depends(get_db)) -> Dict[str, Any]:
    ranked = top_risk_companies(db, limit=10)
    rows = []
    for c, score in ranked:
        articles_count = int(
            db.scalar(select(func.count()).select_from(Article).where(Article.company_id == c.id)) or 0
        )
        analyzed = int(
            db.scalar(
                select(func.count())
                .select_from(Article)
                .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
                .where(Article.company_id == c.id)
            )
            or 0
        )
        rows.append(
            {
                "id": str(c.id),
                "name": c.name,
                "nip": c.nip,
                "score": score,
                "articles_count": articles_count,
                "analyzed_articles": analyzed,
            }
        )
    return {"companies": rows}


@router.get("/dashboard/high-risk-articles", response_model=List[TopRiskArticle])
def high_risk_recent(
    hours: int = 48,
    min_severity: float = 5.0,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
) -> List[TopRiskArticle]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(Article, ArticleAnalysis, Company)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .join(Company, Company.id == Article.company_id)
        .where(ArticleAnalysis.analyzed_at >= cutoff)
        .where(ArticleAnalysis.severity.isnot(None))
        .where(ArticleAnalysis.severity >= min_severity)
    )
    if category:
        stmt = stmt.where(ArticleAnalysis.risk_category == category)
    stmt = stmt.order_by(ArticleAnalysis.severity.desc()).limit(50)
    rows = db.execute(stmt).all()
    return [
        TopRiskArticle(
            article_id=a.id,
            company_id=c.id,
            company_name=c.name,
            title=a.title,
            source=a.source,
            published_at=a.published_at,
            severity=float(an.severity) if an.severity is not None else None,
            risk_category=an.risk_category,
        )
        for a, an, c in rows
    ]
