from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.analysis.risk_lexicon import RISK_CATEGORIES
from app.database import get_db
from app.models import Article, ArticleAnalysis, Company, ScanJob, ScoreHistory
from app.scoring.calculator import (
    ensure_initial_snapshot,
    latest_score_for_company,
    recalculate_and_persist,
    recommendation_for,
    score_history_series,
    top_risk_companies,
)
from app.scraper.registry import (
    RegistryRecord,
    guess_aliases_from_name,
    lookup_nip,
    lookup_query,
)
from app.scraper.tasks import run_scan_in_background, synthesize_and_persist_insights

router = APIRouter()


# ─── Pydantic schemas ────────────────────────────────────────────────────────

class CompanyIn(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)
    nip: Optional[str] = None
    krs: Optional[str] = None
    regon: Optional[str] = None
    ticker: Optional[str] = None
    sector: Optional[str] = None
    country: Optional[str] = "PL"


class QuickLookupIn(BaseModel):
    query: str
    scan: bool = True


class QuickLookupOut(BaseModel):
    company_id: str
    created: bool
    from_registry: bool
    registry_record: Optional[Dict[str, Any]] = None
    scan_job_id: Optional[str] = None


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    aliases: Optional[List[str]] = None
    nip: Optional[str] = None
    krs: Optional[str] = None
    regon: Optional[str] = None
    ticker: Optional[str] = None
    sector: Optional[str] = None
    country: Optional[str] = None
    legal_form: Optional[str] = None
    address: Optional[str] = None
    status_vat: Optional[str] = None
    registration_date: Optional[str] = None
    pkd_primary: Optional[str] = None
    pkd_primary_label: Optional[str] = None
    pkd_all: Optional[List[Dict[str, Any]]] = None
    registry_sources: Optional[List[str]] = None
    is_temporary: bool = False
    created_at: datetime


class CompanyDetailOut(CompanyOut):
    current_score: Optional[float] = None
    investment_score: Optional[float] = None
    recommendation: Optional[str] = None
    recommendation_description: Optional[str] = None
    score_components: Optional[Dict[str, Any]] = None
    article_count: int = 0
    analyzed_count: int = 0
    last_scan_at: Optional[datetime] = None

    # ── Unified verdict surfaced for the UI (single source of truth) ───
    verdict_status: Optional[str] = None          # scored | insufficient_evidence | offline_fallback | never_scanned
    analysis_status: Optional[str] = None         # same as verdict_status but includes never_scanned|scanning
    confidence: Optional[str] = None              # low | medium | high
    rationale: Optional[List[str]] = None
    key_concerns: Optional[List[str]] = None
    key_positives: Optional[List[str]] = None
    overrides: Optional[List[str]] = None
    signals: Optional[Dict[str, Any]] = None

    ai_summary: Optional[str] = None
    strengths: Optional[List[str]] = None
    weaknesses: Optional[List[str]] = None
    opportunities: Optional[List[str]] = None
    threats: Optional[List[str]] = None
    investment_thesis: Optional[str] = None
    insights_generated_at: Optional[datetime] = None


class ScorePoint(BaseModel):
    timestamp: datetime
    score: float
    investment_score: Optional[float] = None
    recommendation: Optional[str] = None
    article_count: int


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    title: Optional[str]
    source: Optional[str]
    published_at: Optional[datetime]
    language: Optional[str]
    sentiment_score: Optional[float]
    sentiment_label: Optional[str]
    risk_level: Optional[str]
    risk_category: Optional[str]
    risk_categories: Optional[List[str]]
    risk_keywords: Optional[List[str]]
    severity: Optional[float]
    investment_impact: Optional[str]
    investment_risk: Optional[float]
    credibility_score: Optional[float]
    is_likely_fake: Optional[bool]
    credibility_notes: Optional[str]
    summary: Optional[str]
    key_facts: Optional[List[str]]
    red_flags: Optional[List[str]]
    positive_points: Optional[List[str]]
    mentions_company: Optional[bool] = None


class ScanStartOut(BaseModel):
    job_id: str
    company_id: str
    status: str


class ScanJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    status: str
    message: Optional[str]
    sources_found: int
    articles_analyzed: int
    started_at: datetime
    finished_at: Optional[datetime]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _company_detail(db: Session, c: Company, *, lazy: bool = False) -> CompanyDetailOut:
    # When called from list endpoints we skip ensure_initial_snapshot to avoid
    # firing Claude for every not-yet-scanned company on every page load.
    latest = latest_score_for_company(db, c.id) if lazy else ensure_initial_snapshot(db, c.id)
    article_count = int(db.scalar(select(func.count()).select_from(Article).where(Article.company_id == c.id)) or 0)
    analyzed = int(
        db.scalar(
            select(func.count())
            .select_from(Article)
            .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
            .where(Article.company_id == c.id)
        )
        or 0
    )
    last_scan = db.scalar(
        select(Article.scraped_at)
        .where(Article.company_id == c.id)
        .order_by(Article.scraped_at.desc())
        .limit(1)
    )
    components = latest.score_components if latest else None
    rec = latest.recommendation if latest else None
    rec_desc = None
    verdict_status = None
    confidence = None
    rationale = None
    key_concerns = None
    key_positives = None
    overrides = None
    signals = None
    if components:
        rec_desc = components.get("recommendation_description")
        verdict_status = components.get("status")
        confidence = components.get("confidence")
        rationale = components.get("rationale")
        key_concerns = components.get("key_concerns")
        key_positives = components.get("key_positives")
        overrides = components.get("overrides")
        signals = components.get("signals")
    if not rec_desc and rec:
        _, rec_desc = recommendation_for(float(latest.score) if latest else 0.0)

    if verdict_status is None:
        analysis_status = "never_scanned" if latest is None else "scored"
    else:
        analysis_status = verdict_status

    return CompanyDetailOut(
        id=c.id,
        name=c.name,
        aliases=c.aliases,
        nip=c.nip,
        krs=c.krs,
        regon=c.regon,
        ticker=c.ticker,
        sector=c.sector,
        country=c.country,
        legal_form=c.legal_form,
        address=c.address,
        status_vat=c.status_vat,
        registration_date=c.registration_date,
        pkd_primary=c.pkd_primary,
        pkd_primary_label=c.pkd_primary_label,
        pkd_all=c.pkd_all,
        registry_sources=c.registry_sources,
        is_temporary=c.is_temporary,
        created_at=c.created_at,
        current_score=float(latest.score) if latest else None,
        investment_score=float(latest.investment_score) if latest and latest.investment_score is not None else None,
        recommendation=rec,
        recommendation_description=rec_desc,
        score_components=components,
        article_count=article_count,
        analyzed_count=analyzed,
        last_scan_at=last_scan,
        verdict_status=verdict_status,
        analysis_status=analysis_status,
        confidence=confidence,
        rationale=rationale,
        key_concerns=key_concerns,
        key_positives=key_positives,
        overrides=overrides,
        signals=signals,
        ai_summary=c.ai_summary,
        strengths=c.strengths,
        weaknesses=c.weaknesses,
        opportunities=c.opportunities,
        threats=c.threats,
        investment_thesis=c.investment_thesis,
        insights_generated_at=c.insights_generated_at,
    )


# ─── Company CRUD ────────────────────────────────────────────────────────────

@router.get("/api/companies", response_model=List[CompanyDetailOut])
def list_companies(db: Session = Depends(get_db)) -> List[CompanyDetailOut]:
    companies = list(db.scalars(select(Company).order_by(Company.name)).all())
    return [_company_detail(db, c, lazy=True) for c in companies]


@router.post("/api/companies", response_model=CompanyDetailOut, status_code=201)
def create_company(payload: CompanyIn, db: Session = Depends(get_db)) -> CompanyDetailOut:
    c = Company(
        name=payload.name.strip(),
        aliases=[a.strip() for a in payload.aliases if a.strip()] or None,
        nip=payload.nip.strip() if payload.nip else None,
        krs=payload.krs.strip() if payload.krs else None,
        regon=payload.regon.strip() if payload.regon else None,
        ticker=payload.ticker.strip().upper() if payload.ticker else None,
        sector=payload.sector.strip() if payload.sector else None,
        country=(payload.country or "PL").upper(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    if c.nip and not c.regon:
        try:
            rec = lookup_nip(c.nip)
            if rec:
                _apply_registry(c, rec)
                db.commit()
                db.refresh(c)
        except Exception:
            pass

    return _company_detail(db, c)


def _apply_registry(company: Company, rec: RegistryRecord) -> None:
    if rec.name and not company.name:
        company.name = rec.name
    if rec.nip and not company.nip:
        company.nip = rec.nip
    if rec.regon and not company.regon:
        company.regon = rec.regon
    if rec.krs and not company.krs:
        company.krs = rec.krs
    if rec.legal_form and not company.legal_form:
        company.legal_form = rec.legal_form
    if rec.status_vat:
        company.status_vat = rec.status_vat
    if rec.address and not company.address:
        company.address = rec.address
    if rec.registration_date and not company.registration_date:
        company.registration_date = rec.registration_date
    if rec.pkd_primary and not company.pkd_primary:
        company.pkd_primary = rec.pkd_primary
    if rec.pkd_primary_label and not company.pkd_primary_label:
        company.pkd_primary_label = rec.pkd_primary_label
    if rec.pkd_all and not company.pkd_all:
        company.pkd_all = rec.pkd_all
    # Auto-fill sector from PKD label if missing — helps Claude's SWOT step.
    if rec.pkd_primary_label and not company.sector:
        company.sector = rec.pkd_primary_label[:128]
    # Registry sources — merge, preserving order
    existing_srcs = list(company.registry_sources or [])
    for s in rec.sources or []:
        if s not in existing_srcs:
            existing_srcs.append(s)
    if existing_srcs:
        company.registry_sources = existing_srcs
    meta = dict(company.registry_meta or {})
    for k, v in (rec.raw or {}).items():
        meta.setdefault(k, v)
    if meta:
        company.registry_meta = meta
    if rec.name:
        existing_aliases = list(company.aliases or [])
        for a in guess_aliases_from_name(rec.name):
            if a and a not in existing_aliases:
                existing_aliases.append(a)
        if existing_aliases:
            company.aliases = existing_aliases


_IDENT_PREFIX_RE = re.compile(r"^\s*(?:nip|krs|regon)\s*[:=]?\s*", re.IGNORECASE)


def _is_identifier_query(q: str) -> bool:
    """True when the query is a pure NIP/REGON/KRS number (with optional prefix)."""
    s = _IDENT_PREFIX_RE.sub("", q).strip()
    if not s:
        return False
    digits = re.sub(r"\D", "", s)
    if not digits:
        return False
    # allow dashes/spaces but no letters between digits
    non_digit = sum(1 for c in s if not c.isdigit() and not c.isspace() and c not in "-")
    if non_digit > 0:
        return False
    return len(digits) in (9, 10, 14)  # 9=REGON-9, 10=NIP or KRS, 14=REGON-14


@router.post("/api/companies/quick-lookup", response_model=QuickLookupOut)
def quick_lookup(
    payload: QuickLookupIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> QuickLookupOut:
    """Instant company lookup.

    Flow:
      1. Parse the query — if it's a structured identifier (NIP/REGON/KRS),
         resolve it in registries (MF white-list / KRS / CEIDG / GUS) and use
         the resolved company *name* as the persisted `Company.name`.
      2. If it's a free-text name, use it verbatim and optionally try registry
         enrichment in the background.
      3. Never persist a bare identifier as the company display name — if the
         registries don't know it, fail fast with a helpful 404.
      4. A scan (articles + AI verdict) is kicked off in the background so
         that the registry data is displayed immediately in the UI.
    """
    q = (payload.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Puste zapytanie")

    is_id = _is_identifier_query(q)

    # Try registry lookup. For identifiers this is the only reliable way to
    # obtain a readable company name; for text queries it's a best-effort.
    rec = lookup_query(q)

    # Reject identifiers we can't resolve — don't save the KRS/NIP as a fake
    # company name. This fixes the "history shows 0000028860 instead of
    # PKN Orlen" bug.
    if is_id and (rec is None or not (rec.name or "").strip()):
        raise HTTPException(
            status_code=404,
            detail=(
                "Nie znaleziono firmy w rejestrach (MF / KRS / CEIDG) dla podanego "
                "numeru. Sprawdź poprawność NIP / KRS / REGON lub spróbuj wpisać "
                "nazwę spółki."
            ),
        )

    # Resolve canonical display name.
    if rec and (rec.name or "").strip():
        display_name = rec.name.strip()
    else:
        display_name = q  # free-text name typed by the user

    # Find existing DB row: prefer NIP, then KRS, then case-insensitive name match.
    existing: Optional[Company] = None
    if rec and rec.nip:
        existing = db.scalar(select(Company).where(Company.nip == rec.nip))
    if existing is None and rec and rec.krs:
        existing = db.scalar(select(Company).where(Company.krs == rec.krs))
    if existing is None:
        existing = db.scalar(select(Company).where(Company.name.ilike(display_name)))

    created = False
    if existing is None:
        existing = Company(
            name=display_name,
            country="PL",
            is_temporary=False,
            aliases=guess_aliases_from_name(display_name) or None,
        )
        db.add(existing)
        created = True
    else:
        # If the existing row had a placeholder identifier-as-name stored from
        # a previous buggy run, upgrade it to the proper registry name.
        if rec and (rec.name or "").strip() and existing.name != rec.name:
            digits_only = "".join(ch for ch in (existing.name or "") if ch.isdigit())
            if (
                not existing.name
                or existing.name.strip() == q
                or digits_only == existing.name.replace(" ", "").replace("-", "")
            ):
                existing.name = rec.name.strip()

    if rec:
        _apply_registry(existing, rec)

    db.commit()
    db.refresh(existing)

    job_id: Optional[str] = None
    if payload.scan:
        job = ScanJob(company_id=existing.id, status="pending")
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        background_tasks.add_task(run_scan_in_background, existing.id, job.id)

    return QuickLookupOut(
        company_id=existing.id,
        created=created,
        from_registry=bool(rec),
        registry_record=(rec.to_dict() if rec else None),
        scan_job_id=job_id,
    )


@router.get("/api/registry/lookup")
def registry_lookup(q: str = Query(..., min_length=3)) -> Dict[str, Any]:
    """Read-only registry lookup, without creating a company."""
    rec = lookup_query(q)
    if not rec:
        return {"found": False, "query": q}
    return {"found": True, "query": q, "record": rec.to_dict()}


@router.get("/api/companies/{company_id}", response_model=CompanyDetailOut)
def get_company(company_id: str, db: Session = Depends(get_db)) -> CompanyDetailOut:
    c = db.get(Company, company_id)
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")
    return _company_detail(db, c)


@router.delete("/api/companies/{company_id}", status_code=204)
def delete_company(company_id: str, db: Session = Depends(get_db)) -> None:
    c = db.get(Company, company_id)
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")
    db.delete(c)
    db.commit()


@router.get("/api/search", response_model=List[CompanyDetailOut])
def search_companies(q: str = Query(..., min_length=1), db: Session = Depends(get_db)) -> List[CompanyDetailOut]:
    needle = f"%{q.strip()}%"
    direct = list(
        db.scalars(
            select(Company).where(or_(Company.name.ilike(needle), Company.nip.ilike(needle), Company.ticker.ilike(needle)))
        ).all()
    )
    if direct:
        return [_company_detail(db, c, lazy=True) for c in direct]

    all_c = list(db.scalars(select(Company)).all())
    ranked: List[Tuple[Company, int]] = []
    ql = q.strip().lower()
    for c in all_c:
        score = fuzz.partial_ratio(ql, c.name.lower())
        if c.aliases:
            score = max(score, max((fuzz.partial_ratio(ql, a.lower()) for a in c.aliases), default=0))
        if c.nip:
            score = max(score, fuzz.partial_ratio(ql, c.nip.lower()))
        if score >= 70:
            ranked.append((c, score))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [_company_detail(db, c, lazy=True) for c, _ in ranked[:25]]


# ─── Score history ───────────────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/score/history", response_model=List[ScorePoint])
def score_history(company_id: str, days: int = 90, db: Session = Depends(get_db)) -> List[ScorePoint]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    rows = score_history_series(db, company_id, days=days)
    return [
        ScorePoint(
            timestamp=r.timestamp,
            score=float(r.score),
            investment_score=float(r.investment_score) if r.investment_score is not None else None,
            recommendation=r.recommendation,
            article_count=r.article_count,
        )
        for r in rows
    ]


@router.post("/api/companies/{company_id}/recalculate")
def recalculate(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    snap = recalculate_and_persist(db, company_id, lookback_days=90)
    return {
        "score": float(snap.score),
        "investment_score": float(snap.investment_score or 0.0),
        "recommendation": snap.recommendation,
    }


# ─── Articles ────────────────────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/articles", response_model=List[ArticleOut])
def company_articles(
    company_id: str,
    limit: int = 100,
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
                id=str(a.id),
                url=a.url,
                title=a.title,
                source=a.source,
                published_at=a.published_at,
                language=a.language,
                sentiment_score=float(an.sentiment_score) if an and an.sentiment_score is not None else None,
                sentiment_label=an.sentiment_label if an else None,
                risk_level=an.risk_level if an else None,
                risk_category=an.risk_category if an else None,
                risk_categories=an.risk_categories if an else None,
                risk_keywords=an.risk_keywords if an else None,
                severity=float(an.severity) if an and an.severity is not None else None,
                investment_impact=an.investment_impact if an else None,
                investment_risk=float(an.investment_risk) if an and an.investment_risk is not None else None,
                credibility_score=float(an.credibility_score) if an and an.credibility_score is not None else None,
                is_likely_fake=bool(an.is_likely_fake) if an and an.is_likely_fake is not None else None,
                credibility_notes=an.credibility_notes if an else None,
                summary=an.summary if an else None,
                key_facts=an.key_facts if an else None,
                red_flags=an.red_flags if an else None,
                positive_points=an.positive_points if an else None,
                mentions_company=(
                    bool(an.mentions_company)
                    if an and an.mentions_company is not None
                    else None
                ),
            )
        )
    return out


@router.post("/api/companies/{company_id}/synthesize", response_model=CompanyDetailOut)
def synthesize_insights(company_id: str, db: Session = Depends(get_db)) -> CompanyDetailOut:
    """Regenerate the SWOT / investment thesis for a company (re-runs Claude)."""
    c = db.get(Company, company_id)
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")
    synthesize_and_persist_insights(db, company_id)
    db.refresh(c)
    return _company_detail(db, c)


# ─── Scanning (background) ───────────────────────────────────────────────────

@router.post("/api/companies/{company_id}/scan", response_model=ScanStartOut)
def trigger_scan(
    company_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ScanStartOut:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    job = ScanJob(company_id=company_id, status="pending")
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_scan_in_background, company_id, job.id)
    return ScanStartOut(job_id=job.id, company_id=company_id, status=job.status)


@router.get("/api/scans/{job_id}", response_model=ScanJobOut)
def get_scan_status(job_id: str, db: Session = Depends(get_db)) -> ScanJob:
    job = db.get(ScanJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/api/companies/{company_id}/scans", response_model=List[ScanJobOut])
def list_company_scans(company_id: str, limit: int = 10, db: Session = Depends(get_db)) -> List[ScanJob]:
    if not db.get(Company, company_id):
        raise HTTPException(status_code=404, detail="Company not found")
    stmt = (
        select(ScanJob)
        .where(ScanJob.company_id == company_id)
        .order_by(ScanJob.started_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


# ─── Dashboard aggregates ────────────────────────────────────────────────────

@router.get("/api/dashboard/overview")
def dashboard_overview(db: Session = Depends(get_db)) -> Dict[str, Any]:
    companies_total = int(db.scalar(select(func.count()).select_from(Company)) or 0)
    articles_total = int(db.scalar(select(func.count()).select_from(Article)) or 0)
    analyzed_total = int(db.scalar(select(func.count()).select_from(ArticleAnalysis)) or 0)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    articles_24h = int(
        db.scalar(select(func.count()).select_from(Article).where(Article.scraped_at >= cutoff)) or 0
    )
    top = top_risk_companies(db, limit=10)
    high_risk_companies = sum(1 for r in top if r["score"] >= 65 or r["investment_score"] >= 65)
    avg_score = 0.0
    if top:
        avg_score = round(sum(r["score"] for r in top) / len(top), 1)

    # Distribution of risk levels among analysed articles
    level_rows = db.execute(
        select(ArticleAnalysis.risk_level, func.count()).group_by(ArticleAnalysis.risk_level)
    ).all()
    level_distribution = {str(lvl or "none"): int(cnt) for lvl, cnt in level_rows}

    # Category distribution (use risk_category)
    cat_rows = db.execute(
        select(ArticleAnalysis.risk_category, func.count())
        .where(ArticleAnalysis.risk_category.isnot(None))
        .group_by(ArticleAnalysis.risk_category)
    ).all()
    category_distribution = {str(c): int(n) for c, n in cat_rows}

    return {
        "companies_total": companies_total,
        "articles_total": articles_total,
        "articles_analyzed": analyzed_total,
        "articles_last_24h": articles_24h,
        "high_risk_companies": high_risk_companies,
        "average_score": avg_score,
        "top_risks": top,
        "risk_level_distribution": level_distribution,
        "risk_category_distribution": category_distribution,
    }


@router.get("/api/dashboard/high-risk-articles")
def high_risk_recent(
    hours: int = 72,
    min_severity: float = 5.0,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
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
    stmt = stmt.order_by(ArticleAnalysis.severity.desc()).limit(100)
    rows = db.execute(stmt).all()
    return [
        {
            "article_id": str(a.id),
            "company_id": c.id,
            "company_name": c.name,
            "title": a.title,
            "url": a.url,
            "source": a.source,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "severity": float(an.severity) if an.severity is not None else None,
            "risk_level": an.risk_level,
            "risk_category": an.risk_category,
            "summary": an.summary,
            "red_flags": an.red_flags,
        }
        for a, an, c in rows
    ]


@router.get("/api/risk-categories")
def risk_categories() -> Dict[str, Any]:
    return {
        "categories": [
            {"id": k, "label": v["label"], "weight": v["weight"]}
            for k, v in RISK_CATEGORIES.items()
        ]
    }
