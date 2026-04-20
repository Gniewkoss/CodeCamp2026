"""Scanning pipeline: collect -> extract -> Claude analyse -> persist -> rescore."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.claude_analyzer import (
    Analysis,
    analyze_article_with_claude,
    synthesize_company_insights,
)
from app.analysis.event_detector import detect_events_from_article
from app.config import get_settings
from app.database import SessionLocal
from app.models import Article, ArticleAnalysis, Company, ScanJob
from app.scoring.calculator import recalculate_and_persist
from app.scraper.extractor import fetch_article_text, infer_language
from app.scraper.sources import RawArticle, collect_all_sources
from app.services.registry_sync import sync_all_registries
from app.services.sanctions_sync import apply_sanctions_check

logger = logging.getLogger(__name__)


def _ensure_article(db: Session, company: Company, raw: RawArticle) -> Optional[Article]:
    existing = db.scalar(
        select(Article).where(Article.company_id == company.id, Article.url == raw.url)
    )
    if existing:
        return existing
    art = Article(
        company_id=company.id,
        url=raw.url,
        title=raw.title,
        content=raw.summary,
        source=raw.source,
        published_at=raw.published_at,
        scraped_at=datetime.now(timezone.utc),
        language=raw.language or "pl",
    )
    db.add(art)
    try:
        db.commit()
        db.refresh(art)
        return art
    except Exception:
        db.rollback()
        return db.scalar(
            select(Article).where(Article.company_id == company.id, Article.url == raw.url)
        )


def _enrich_content(db: Session, article: Article) -> Article:
    if article.content and len(article.content) > 350:
        return article
    extracted = fetch_article_text(article.url)
    if extracted.text:
        article.content = extracted.text
    if extracted.title and not article.title:
        article.title = extracted.title
    if not article.language:
        article.language = extracted.language or infer_language(article.content or "")
    db.add(article)
    try:
        db.commit()
        db.refresh(article)
    except Exception:
        db.rollback()
    return article


def _persist_analysis(db: Session, article: Article, result: Analysis) -> None:
    row = article.analysis
    if row is None:
        row = ArticleAnalysis(article_id=article.id)
        db.add(row)

    row.mentions_company = result.mentions_company
    row.sentiment_score = result.sentiment_score
    row.sentiment_label = result.sentiment_label
    row.risk_level = result.risk_level
    row.risk_category = result.risk_category
    row.risk_categories = result.risk_categories or None
    row.risk_keywords = result.risk_keywords or None
    row.severity = result.severity
    row.investment_impact = result.investment_impact
    row.investment_risk = result.investment_risk
    row.credibility_score = result.credibility_score
    row.is_likely_fake = result.is_likely_fake
    row.credibility_notes = result.credibility_notes or None
    row.summary = result.summary
    row.key_facts = result.key_facts or None
    row.red_flags = result.red_flags or None
    row.positive_points = result.positive_points or None
    row.raw_llm_response = result.raw_llm_response
    row.analyzed_at = datetime.now(timezone.utc)
    db.commit()


def _detect_events_after_analysis(db: Session, article: Article, company: Company) -> None:
    an = db.scalar(select(ArticleAnalysis).where(ArticleAnalysis.article_id == article.id))
    if an:
        try:
            detect_events_from_article(db, article, an, company)
        except Exception as e:
            logger.warning("Event detection failed: %s", e)


def analyze_article_sync(db: Session, article_id: str) -> None:
    article = db.get(Article, article_id)
    if not article:
        return
    company = db.get(Company, article.company_id)
    if not company:
        return
    article = _enrich_content(db, article)
    result = analyze_article_with_claude(
        company_name=company.name,
        aliases=company.aliases or [],
        title=article.title,
        content=article.content,
        source=article.source,
        published_at=article.published_at.isoformat() if article.published_at else None,
    )
    _persist_analysis(db, article, result)


def scrape_company_sync(db: Session, company_id: str, *, job: Optional[ScanJob] = None) -> dict:
    company = db.get(Company, company_id)
    if not company:
        return {"error": "company not found"}
    settings = get_settings()
    name = company.name
    aliases = company.aliases or []
    raws = collect_all_sources(name, aliases if aliases else None, limit=settings.max_articles_per_scan)

    if job:
        job.status = "running"
        job.sources_found = len(raws)
        db.commit()

    try:
        sync_all_registries(db, company_id)
    except Exception as e:
        logger.info("Registry sync skipped: %s", e)

    analyzed = 0
    for raw in raws:
        art = _ensure_article(db, company, raw)
        if not art:
            continue
        art = _enrich_content(db, art)
        try:
            result = analyze_article_with_claude(
                company_name=company.name,
                aliases=company.aliases or [],
                title=art.title,
                content=art.content,
                source=art.source,
                published_at=art.published_at.isoformat() if art.published_at else None,
            )
            _persist_analysis(db, art, result)
            _detect_events_after_analysis(db, art, company)
            analyzed += 1
            if job:
                job.articles_analyzed = analyzed
                db.commit()
        except Exception as e:
            logger.exception("Analysis failed for %s: %s", art.url, e)

    try:
        apply_sanctions_check(db, company_id)
    except Exception as e:
        logger.warning("Sanctions screening skipped: %s", e)

    # Always compute + persist a verdict snapshot (even on zero articles -> insufficient_evidence)
    snap = recalculate_and_persist(db, company_id, lookback_days=90)

    # Second Claude pass — aggregate SWOT / investment thesis (safe to skip on thin data)
    try:
        if analyzed > 0:
            synthesize_and_persist_insights(db, company_id)
    except Exception as e:
        logger.warning("Synth failed for %s: %s", company_id, e)

    if job:
        job.status = "done"
        job.finished_at = datetime.now(timezone.utc)
        verdict_status = (snap.score_components or {}).get("status") if snap else None
        if verdict_status == "insufficient_evidence":
            job.message = (
                f"Brak wiarygodnych dowodów ({len(raws)} artykułów / 0 dopasowanych). "
                "Dodaj aliasy nazwy i ponów skan."
            )
        else:
            job.message = f"Przetworzono {analyzed}/{len(raws)} artykułów."
        db.commit()

    return {"sources_found": len(raws), "articles_analyzed": analyzed}


def synthesize_and_persist_insights(db: Session, company_id: str) -> None:
    """Aggregate all analysed articles into a SWOT + investment thesis and cache on Company."""
    company = db.get(Company, company_id)
    if not company:
        return
    stmt = (
        select(Article, ArticleAnalysis)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .where(Article.company_id == company_id)
        .where(ArticleAnalysis.mentions_company.is_(True))
        .order_by(Article.scraped_at.desc())
        .limit(40)
    )
    rows = db.execute(stmt).all()
    payload = []
    for art, an in rows:
        payload.append(
            {
                "title": art.title,
                "source": art.source,
                "published_at": art.published_at.isoformat() if art.published_at else None,
                "sentiment_score": float(an.sentiment_score) if an.sentiment_score is not None else None,
                "risk_level": an.risk_level,
                "risk_categories": an.risk_categories,
                "severity": float(an.severity) if an.severity is not None else None,
                "credibility_score": float(an.credibility_score) if an.credibility_score is not None else None,
                "is_likely_fake": an.is_likely_fake,
                "investment_impact": an.investment_impact,
                "summary": an.summary,
                "key_facts": an.key_facts,
                "red_flags": an.red_flags,
                "positive_points": an.positive_points,
            }
        )
    insights = synthesize_company_insights(
        company_name=company.name,
        sector=company.sector,
        analyses=payload,
    )
    company.ai_summary = insights.ai_summary or None
    company.strengths = insights.strengths or None
    company.weaknesses = insights.weaknesses or None
    company.opportunities = insights.opportunities or None
    company.threats = insights.threats or None
    company.investment_thesis = insights.investment_thesis or None
    company.insights_generated_at = datetime.now(timezone.utc)
    db.commit()


def run_scan_in_background(company_id: str, job_id: str) -> None:
    """Called via FastAPI BackgroundTasks — creates its own DB session."""
    db = SessionLocal()
    try:
        job = db.get(ScanJob, job_id)
        try:
            scrape_company_sync(db, company_id, job=job)
        except Exception as e:
            logger.exception("Scan failed: %s", e)
            if job:
                job.status = "error"
                job.message = str(e)[:500]
                job.finished_at = datetime.now(timezone.utc)
                db.commit()
            # Even on failure, leave a verdict snapshot so the company is visible
            try:
                recalculate_and_persist(db, company_id, lookback_days=90)
            except Exception as inner:
                logger.warning("Fallback verdict after failure skipped: %s", inner)
    finally:
        db.close()
