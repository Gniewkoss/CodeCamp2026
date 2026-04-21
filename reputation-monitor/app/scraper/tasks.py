"""Scanning pipeline: collect -> extract -> Claude analyse -> persist -> rescore."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return ``dt`` as an offset-aware UTC datetime.

    SQLite strips timezone info on read, so timestamps we persisted as UTC
    come back naive. Treat any naive datetime as UTC; convert aware ones
    to UTC. Returns ``None`` for ``None``.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
    if not an:
        return
    # CRITICAL: only detect events on articles Claude confirmed are actually
    # about this company. Otherwise we were creating fake RiskEvents like
    # "Śledztwo prokuratorskie ws. Zondacrypto" for mBank, because Claude
    # extracted events from an article that *mentioned* banking but was
    # about a different entity.
    if an.mentions_company is False:
        return
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


def _set_stage(db: Session, job: Optional[ScanJob], stage: str, detail: str = "") -> None:
    if job is None:
        return
    job.stage = stage
    job.stage_detail = detail or None
    try:
        db.commit()
    except Exception:
        db.rollback()


def scrape_company_sync(db: Session, company_id: str, *, job: Optional[ScanJob] = None) -> dict:
    company = db.get(Company, company_id)
    if not company:
        return {"error": "company not found"}
    settings = get_settings()
    name = company.name
    aliases = company.aliases or []

    if job:
        job.status = "running"
        db.commit()

    _set_stage(db, job, "scraping", "Pobieram artykuły z Google News, NewsAPI, RSS i GDELT…")
    raws = collect_all_sources(name, aliases if aliases else None, limit=settings.max_articles_per_scan)

    if job:
        job.sources_found = len(raws)
        db.commit()

    _set_stage(db, job, "registry", "Synchronizuję dane z KRS / CEIDG / MF…")
    try:
        sync_all_registries(db, company_id)
    except Exception as e:
        logger.info("Registry sync skipped: %s", e)

    _set_stage(
        db, job, "analyzing",
        f"Claude analizuje {len(raws)} artykułów równolegle (×{settings.analysis_workers}) — "
        f"sentyment, red flags, wiarygodność…",
    )

    analyzed = 0
    reused = 0
    skipped = 0
    failed = 0
    deadline_hit = False
    cooldown = timedelta(hours=settings.reanalysis_cooldown_hours)
    now = datetime.now(timezone.utc)

    # ── Step 1: materialise Article rows + reuse-from-cache decisions ──
    # We do this serially because SQLAlchemy sessions are not thread-safe,
    # and DB writes here are cheap (no network). Articles flagged as "fresh"
    # will be shipped to Claude in parallel below.
    to_analyse: list[Article] = []
    for raw in raws:
        art = _ensure_article(db, company, raw)
        if not art:
            skipped += 1
            continue
        art = _enrich_content(db, art)
        existing = art.analysis
        existing_at = _as_utc(existing.analyzed_at) if existing else None
        if (
            existing is not None
            and existing_at is not None
            and (now - existing_at) < cooldown
        ):
            analyzed += 1
            reused += 1
            continue
        if not (art.content or art.title):
            skipped += 1
            continue
        to_analyse.append(art)

    if job:
        job.articles_analyzed = analyzed
        job.stage_detail = (
            f"cache: {reused} · do analizy: {len(to_analyse)} · "
            f"workers: {settings.analysis_workers}"
        )
        db.commit()

    # ── Step 2: fan out Claude analysis across a thread pool ──
    # The Anthropic SDK blocks on I/O, so threads are the right tool — we
    # don't need asyncio and don't need per-thread SQLAlchemy sessions.
    deadline_at = time.monotonic() + max(30, settings.analysis_deadline_seconds)

    # Snapshot everything Claude needs BEFORE fanning out, so worker threads
    # never touch the SQLAlchemy session (which isn't thread-safe). We key
    # back to the Article row on the main thread for persistence.
    company_name_snap = company.name
    aliases_snap = list(company.aliases or [])
    snapshots: list[tuple[Article, dict]] = [
        (
            art,
            {
                "title": art.title,
                "content": art.content,
                "source": art.source,
                "published_at": art.published_at.isoformat() if art.published_at else None,
            },
        )
        for art in to_analyse
    ]

    def _run_claude(snap: dict):
        return analyze_article_with_claude(
            company_name=company_name_snap,
            aliases=aliases_snap,
            title=snap["title"],
            content=snap["content"],
            source=snap["source"],
            published_at=snap["published_at"],
        )

    if snapshots:
        workers = max(1, min(settings.analysis_workers, len(snapshots)))
        total = len(snapshots)
        done = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="claude") as pool:
            futures = {pool.submit(_run_claude, snap): art for art, snap in snapshots}
            try:
                for fut in as_completed(futures):
                    art = futures[fut]
                    done += 1
                    if time.monotonic() > deadline_at:
                        # Cancel everything that hasn't started yet and break
                        # out of the loop. Claude calls already in-flight will
                        # complete (can't reliably cancel an httpx.post mid-
                        # roundtrip), but we'll stop waiting on them.
                        deadline_hit = True
                        for f2 in futures:
                            if not f2.done():
                                f2.cancel()
                        logger.warning(
                            "Analysis deadline %ds hit for %s — %d/%d done, rest skipped",
                            settings.analysis_deadline_seconds, company_id, done, total,
                        )
                        break
                    try:
                        result = fut.result()
                    except Exception as e:
                        failed += 1
                        logger.warning("Analysis failed for %s: %s", art.url, e)
                        continue
                    try:
                        _persist_analysis(db, art, result)
                        _detect_events_after_analysis(db, art, company)
                        analyzed += 1
                    except Exception as e:
                        failed += 1
                        logger.exception("Persist failed for %s: %s", art.url, e)
                        continue
                    if job and (done % 3 == 0 or done == total):
                        job.articles_analyzed = analyzed
                        job.stage_detail = (
                            f"{done}/{total} · Claude ×{workers} · ok={analyzed} "
                            f"err={failed}"
                        )
                        try:
                            db.commit()
                        except Exception:
                            db.rollback()
            finally:
                # Don't wait for stragglers past the deadline.
                pool.shutdown(wait=not deadline_hit, cancel_futures=True)

    logger.info(
        "Scan %s: %d sources, %d analysed (%d reused, %d failed, %d skipped%s)",
        company_id, len(raws), analyzed, reused, failed, skipped,
        ", DEADLINE HIT" if deadline_hit else "",
    )

    _set_stage(db, job, "events", "Sprawdzam sankcje UE/OFAC i listę konsolidowaną MSW…")
    try:
        apply_sanctions_check(db, company_id)
    except Exception as e:
        logger.warning("Sanctions screening skipped: %s", e)

    # ── Financial / commercial / governance pillars ─────────────────────
    # Each helper enforces its own cooldown, so re-running the scan within
    # the refresh window will be a no-op for these steps.
    from app.analysis import financial_pipeline as fp  # local import keeps import graph tidy

    _set_stage(db, job, "financials", "Pobieram sprawozdania finansowe (KRS RDF / wiedza AI)…")
    try:
        fp.refresh_financial_statements(db, company)
        fp.refresh_financial_ratios(db, company)
    except Exception as e:
        logger.warning("Financials stage failed: %s", e)

    _set_stage(db, job, "balance_ai", "Claude analizuje bilans ekonomiczny z 3 lat…")
    try:
        fp.refresh_balance_ai(db, company)
    except Exception as e:
        logger.warning("BalanceAI stage failed: %s", e)

    _set_stage(db, job, "contracts", "Szukam kontraktów publicznych (TED / BZP / prasa)…")
    try:
        fp.refresh_contracts(db, company)
    except Exception as e:
        logger.warning("Contracts stage failed: %s", e)

    _set_stage(db, job, "insurance", "Badam sygnały ubezpieczenia należności…")
    try:
        fp.refresh_insurance(db, company, aliases=list(aliases or []))
    except Exception as e:
        logger.warning("Insurance stage failed: %s", e)

    _set_stage(db, job, "payments", "Opinia rynkowa — terminowość płatności…")
    try:
        fp.refresh_payments(db, company)
    except Exception as e:
        logger.warning("Payments stage failed: %s", e)

    _set_stage(db, job, "governance", "Sprawdzam historię osób z KRS…")
    try:
        fp.refresh_governance(db, company)
    except Exception as e:
        logger.warning("Governance stage failed: %s", e)

    _set_stage(db, job, "regulatory", "Parsuję KRS Dział 6 i MSiG / KRZ…")
    try:
        fp.refresh_regulatory(db, company)
    except Exception as e:
        logger.warning("Regulatory stage failed: %s", e)

    _set_stage(db, job, "limit", "Liczę rekomendowany limit kupiecki…")
    try:
        fp.refresh_trade_credit_limit(db, company)
    except Exception as e:
        logger.warning("Trade credit limit failed: %s", e)

    _set_stage(db, job, "verdict", "Claude formułuje ostateczny werdykt z sygnałów i zdarzeń…")
    snap = recalculate_and_persist(db, company_id, lookback_days=90)

    _set_stage(db, job, "synth", "Claude generuje SWOT i tezę inwestycyjną…")
    try:
        if analyzed > 0:
            synthesize_and_persist_insights(db, company_id)
    except Exception as e:
        logger.warning("Synth failed for %s: %s", company_id, e)

    # Count analyses actually present in the DB for this company — this is
    # the number the UI should trust even if our in-memory ``analyzed`` drift
    # (rollbacks, commits in other transactions, etc.) lost a few increments.
    try:
        from sqlalchemy import func

        db_analyzed = int(
            db.scalar(
                select(func.count(ArticleAnalysis.id))
                .join(Article, ArticleAnalysis.article_id == Article.id)
                .where(Article.company_id == company_id)
            )
            or 0
        )
    except Exception:
        db_analyzed = analyzed

    reported_analyzed = max(analyzed, db_analyzed)

    if job:
        job.status = "done"
        job.stage = "done"
        job.stage_detail = None
        job.finished_at = datetime.now(timezone.utc)
        job.articles_analyzed = reported_analyzed
        verdict_status = (snap.score_components or {}).get("status") if snap else None
        fresh = max(0, analyzed - reused)
        if verdict_status == "insufficient_evidence" and reported_analyzed == 0:
            job.message = (
                f"Brak wiarygodnych dowodów — zebrano {len(raws)} artykułów z sieci, "
                f"żaden nie dotyczył spółki. Dodaj aliasy nazwy lub NIP i ponów skan."
            )
        else:
            parts = [
                f"Zebrano z sieci: {len(raws)}",
                f"przeanalizowanych: {reported_analyzed}",
            ]
            if reused:
                parts.append(f"{reused} z cache ({settings.reanalysis_cooldown_hours}h)")
            if fresh:
                parts.append(f"{fresh} świeżo analizowanych")
            if failed:
                parts.append(f"{failed} błędów Claude")
            if skipped:
                parts.append(f"{skipped} duplikatów URL")
            if deadline_hit:
                parts.append(
                    f"deadline {settings.analysis_deadline_seconds}s — reszta pominięta"
                )
            job.message = " · ".join(parts) + "."
        db.commit()

    return {"sources_found": len(raws), "articles_analyzed": reported_analyzed}


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
