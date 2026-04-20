from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.nlp_pipeline import persist_analysis, run_analysis
from app.database import SessionLocal
from app.models import Article, Company
from app.scraper.extractor import fetch_article_text, infer_language
from app.scraper.sources import RawArticle, collect_all_sources
from app.scoring.calculator import calculate_score

logger = logging.getLogger(__name__)

try:
    from celery import shared_task
except Exception:  # pragma: no cover

    def shared_task(*args, **kwargs):
        def deco(fn):
            return fn

        return deco


def _ensure_article(db: Session, company: Company, raw: RawArticle) -> Article | None:
    existing = db.scalar(select(Article).where(Article.url == raw.url))
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
        return db.scalar(select(Article).where(Article.url == raw.url))


def _enrich_content(db: Session, article: Article) -> Article:
    if article.content and len(article.content) > 200:
        return article
    extracted = fetch_article_text(article.url, use_playwright=False)
    if extracted.text:
        article.content = extracted.text
    if extracted.title and not article.title:
        article.title = extracted.title
    if not article.language:
        article.language = extracted.language or infer_language(article.content or "")
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def analyze_article_sync(db: Session, article_id: uuid.UUID) -> None:
    article = db.get(Article, article_id)
    if not article:
        return
    company = db.get(Company, article.company_id)
    if not company:
        return
    article = _enrich_content(db, article)
    result = run_analysis(
        article,
        company.name,
        company.aliases or [],
        use_llm=True,
    )
    persist_analysis(db, article, result)
    db.commit()


def scrape_company_sync(db: Session, company_id: uuid.UUID) -> dict:
    company = db.get(Company, company_id)
    if not company:
        return {"error": "company not found"}
    name = company.name
    aliases = company.aliases or []
    raws = collect_all_sources(name, aliases if aliases else None)
    analyzed = 0
    for raw in raws:
        art = _ensure_article(db, company, raw)
        if not art:
            continue
        art = _enrich_content(db, art)
        analyze_article_sync(db, art.id)
        analyzed += 1
    calculate_score(company_id, lookback_days=90, db=db, persist=True)
    return {"sources_rows": len(raws), "articles_analyzed": analyzed}


@shared_task(name="app.scraper.tasks.scrape_company")
def scrape_company(company_id: str) -> dict:
    cid = uuid.UUID(company_id)
    db = SessionLocal()
    try:
        return scrape_company_sync(db, cid)
    finally:
        db.close()


@shared_task(name="app.scraper.tasks.scrape_all_companies")
def scrape_all_companies() -> dict:
    db = SessionLocal()
    try:
        companies = list(db.scalars(select(Company)).all())
        summary = {"companies": len(companies), "results": []}
        for c in companies:
            summary["results"].append({"id": str(c.id), **scrape_company_sync(db, c.id)})
        return summary
    finally:
        db.close()


@shared_task(name="app.scraper.tasks.analyze_article")
def analyze_article(article_id: str) -> None:
    aid = uuid.UUID(article_id)
    db = SessionLocal()
    try:
        analyze_article_sync(db, aid)
    finally:
        db.close()
