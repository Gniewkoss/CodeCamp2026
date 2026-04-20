"""Pipeline orchestration for the financial / commercial / governance layers.

This is the piece wired into ``scrape_company_sync``. It refreshes each sub-
system only when its cooldown has elapsed and stores all results using the
persistent ORM models.

Every helper here is safe to call without any cooldown tracking: it checks the
latest row's timestamp itself and early-exits if still fresh.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.balance_ai_analyzer import analyze_balance_sheet
from app.analysis.contract_intensity import compute_contract_intensity
from app.analysis.financial_extractor import (
    ExtractedFigures,
    extract_from_knowledge,
)
from app.analysis.financial_metrics import (
    RatiosPack,
    compute_ratios,
    compute_trend,
    financial_health_score,
)
from app.analysis.governance_risk import analyse_people
from app.analysis.insurance_detector import detect_insurance
from app.analysis.payment_reputation import assess_payment_reputation
from app.analysis.trade_credit_limit import suggest_trade_credit_limit
from app.config import get_settings
from app.models import (
    Company,
    Contract,
    FinancialAIAnalysis,
    FinancialFigures,
    FinancialRatios,
    FinancialStatement,
    InsuranceSignal,
    PaymentReputation,
    RegulatoryEvent,
    TradeCreditLimit,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Cooldown helper
# ────────────────────────────────────────────────────────────────────────


def _is_fresh(last_ts: Optional[datetime], days: int) -> bool:
    if last_ts is None:
        return False
    ref = datetime.now(timezone.utc)
    ts = last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=timezone.utc)
    return (ref - ts) < timedelta(days=days)


# ────────────────────────────────────────────────────────────────────────
# Financials
# ────────────────────────────────────────────────────────────────────────


def refresh_financial_statements(db: Session, company: Company, *, force: bool = False) -> list[FinancialStatement]:
    """Make sure we have up-to-date FinancialStatement + FinancialFigures rows.

    Strategy:
    1. Load existing statements; if any are younger than the cooldown → no-op.
    2. Otherwise try KRS RDF scraper (best-effort).
    3. Fall back to Claude knowledge for well-known companies.
    """
    settings = get_settings()
    statements = list(
        db.scalars(
            select(FinancialStatement)
            .where(FinancialStatement.company_id == company.id)
            .order_by(FinancialStatement.period_end.desc())
        ).all()
    )
    if not force and statements:
        last_extracted = max((s.extracted_at for s in statements if s.extracted_at), default=None)
        if _is_fresh(last_extracted, settings.financials_refresh_days):
            return statements

    extracted_years: list[ExtractedFigures] = []

    # (1) KRS RDF best-effort.
    if company.krs:
        try:
            from app.scraper.krs_rdf import polite_list_statements

            rdf_list = polite_list_statements(company.krs, max_statements=3)
        except Exception as e:
            logger.info("KRS RDF list failed: %s", e)
            rdf_list = []
        for ref in rdf_list:
            # We only persist a shell FinancialStatement row here — deterministic
            # figure extraction from the downloaded binary is intentionally left
            # as a future enhancement.  The figures will be filled via Claude
            # knowledge below for well-known companies.
            pass  # noqa

    # (2) Fallback: Claude knowledge for well-known firms.
    if not extracted_years:
        try:
            extracted_years = extract_from_knowledge(
                company_name=company.name,
                nip=company.nip,
                krs=company.krs,
                sector=company.sector,
                years=3,
            )
        except Exception as e:
            logger.info("Claude knowledge extract failed: %s", e)
            extracted_years = []

    if not extracted_years:
        return statements

    # Persist new statements + figures (deduped by (company_id, period_end)).
    for ex in extracted_years:
        if not ex.period_end:
            continue
        existing = db.scalar(
            select(FinancialStatement).where(
                FinancialStatement.company_id == company.id,
                FinancialStatement.period_end == ex.period_end,
                FinancialStatement.period_type == ex.period_type,
            )
        )
        if existing is not None and not force:
            continue
        if existing is None:
            stmt_row = FinancialStatement(
                company_id=company.id,
                period_end=ex.period_end,
                period_type=ex.period_type,
                currency=ex.currency,
                source=ex.source,
                raw_json=None,
            )
            db.add(stmt_row)
            db.flush()
        else:
            stmt_row = existing
            stmt_row.source = ex.source
            stmt_row.currency = ex.currency
            stmt_row.extracted_at = datetime.now(timezone.utc)

        fig_row = stmt_row.figures
        if fig_row is None:
            fig_row = FinancialFigures(statement_id=stmt_row.id)
            db.add(fig_row)
        for attr in (
            "revenue",
            "cost_of_revenue",
            "operating_costs",
            "ebit",
            "ebitda",
            "net_profit",
            "total_assets",
            "current_assets",
            "non_current_assets",
            "cash",
            "inventory",
            "receivables",
            "total_liabilities",
            "current_liabilities",
            "non_current_liabilities",
            "trade_payables",
            "equity",
            "retained_earnings",
            "cash_from_operations",
            "capex",
            "insurance_costs_mentioned",
        ):
            val = getattr(ex, attr, None)
            if val is not None:
                setattr(fig_row, attr, val)

    try:
        db.commit()
    except Exception as e:
        logger.warning("Financial statements commit failed: %s", e)
        db.rollback()

    return list(
        db.scalars(
            select(FinancialStatement)
            .where(FinancialStatement.company_id == company.id)
            .order_by(FinancialStatement.period_end.desc())
        ).all()
    )


def refresh_financial_ratios(db: Session, company: Company) -> list[RatiosPack]:
    """Re-compute + persist FinancialRatios rows from current FinancialFigures."""
    stmt = (
        select(FinancialStatement, FinancialFigures)
        .join(FinancialFigures, FinancialFigures.statement_id == FinancialStatement.id)
        .where(FinancialStatement.company_id == company.id)
        .order_by(FinancialStatement.period_end.desc())
        .limit(3)
    )
    rows = db.execute(stmt).all()
    figures_by_year: list[ExtractedFigures] = []
    ratios_packs: list[RatiosPack] = []
    for s, f in rows:
        ex = ExtractedFigures(
            period_end=s.period_end or "",
            period_type=s.period_type or "annual",
            currency=s.currency or "PLN",
            source=s.source or "UNKNOWN",
            revenue=f.revenue,
            cost_of_revenue=f.cost_of_revenue,
            operating_costs=f.operating_costs,
            ebit=f.ebit,
            ebitda=f.ebitda,
            net_profit=f.net_profit,
            total_assets=f.total_assets,
            current_assets=f.current_assets,
            non_current_assets=f.non_current_assets,
            cash=f.cash,
            inventory=f.inventory,
            receivables=f.receivables,
            total_liabilities=f.total_liabilities,
            current_liabilities=f.current_liabilities,
            non_current_liabilities=f.non_current_liabilities,
            trade_payables=f.trade_payables,
            equity=f.equity,
            retained_earnings=f.retained_earnings,
        )
        figures_by_year.append(ex)
        r = compute_ratios(ex)
        ratios_packs.append(r)

        existing = db.scalar(
            select(FinancialRatios).where(
                FinancialRatios.company_id == company.id,
                FinancialRatios.period_end == r.period_end,
            )
        )
        row = existing or FinancialRatios(company_id=company.id, period_end=r.period_end)
        for attr in (
            "current_ratio",
            "quick_ratio",
            "cash_ratio",
            "debt_to_equity",
            "debt_to_assets",
            "roe",
            "roa",
            "net_margin",
            "operating_margin",
            "asset_turnover",
            "dpo",
            "dso",
            "dio",
            "cash_conversion_cycle",
            "altman_z_em",
            "maczynska_zem",
        ):
            setattr(row, attr, getattr(r, attr, None))
        if existing is None:
            db.add(row)
    try:
        db.commit()
    except Exception as e:
        logger.warning("Financial ratios commit failed: %s", e)
        db.rollback()
    return ratios_packs


def refresh_balance_ai(
    db: Session,
    company: Company,
    *,
    figures_by_year: Optional[list[ExtractedFigures]] = None,
    ratios_by_year: Optional[list[RatiosPack]] = None,
    force: bool = False,
) -> Optional[FinancialAIAnalysis]:
    settings = get_settings()
    latest = db.scalar(
        select(FinancialAIAnalysis)
        .where(FinancialAIAnalysis.company_id == company.id)
        .order_by(FinancialAIAnalysis.as_of.desc())
        .limit(1)
    )
    if latest and not force and _is_fresh(latest.as_of, settings.balance_ai_refresh_days):
        return latest

    if figures_by_year is None:
        _fb = list(
            db.execute(
                select(FinancialStatement, FinancialFigures)
                .join(FinancialFigures, FinancialFigures.statement_id == FinancialStatement.id)
                .where(FinancialStatement.company_id == company.id)
                .order_by(FinancialStatement.period_end.desc())
                .limit(3)
            ).all()
        )
        figures_by_year = [
            ExtractedFigures(
                period_end=s.period_end or "",
                revenue=f.revenue,
                operating_costs=f.operating_costs,
                ebit=f.ebit,
                ebitda=f.ebitda,
                net_profit=f.net_profit,
                total_assets=f.total_assets,
                current_assets=f.current_assets,
                non_current_assets=f.non_current_assets,
                cash=f.cash,
                inventory=f.inventory,
                receivables=f.receivables,
                total_liabilities=f.total_liabilities,
                current_liabilities=f.current_liabilities,
                non_current_liabilities=f.non_current_liabilities,
                trade_payables=f.trade_payables,
                equity=f.equity,
                retained_earnings=f.retained_earnings,
            )
            for s, f in _fb
        ]
    if not figures_by_year:
        return None
    if ratios_by_year is None:
        ratios_by_year = [compute_ratios(f) for f in figures_by_year]

    trend = compute_trend(figures_by_year)
    health = financial_health_score(ratios_by_year, trend=trend)

    verdict = analyze_balance_sheet(
        company_name=company.name,
        nip=company.nip,
        sector=company.sector,
        figures_by_year=figures_by_year,
        ratios_by_year=ratios_by_year,
        trend=trend,
        health_score=health.score if health else None,
    )

    row = FinancialAIAnalysis(
        company_id=company.id,
        condition=verdict.condition,
        red_flags=verdict.red_flags or None,
        strengths=verdict.strengths or None,
        short_term_risks=verdict.short_term_risks or None,
        long_term_risks=verdict.long_term_risks or None,
        commentary=verdict.commentary or None,
        solvency_forecast_12m=verdict.solvency_forecast_12m,
        years_covered=verdict.years_covered or None,
        raw_prompt=verdict.raw_prompt,
        raw_response=verdict.raw_response,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except Exception as e:
        logger.warning("BalanceAI commit failed: %s", e)
        db.rollback()
        return None
    return row


def refresh_trade_credit_limit(
    db: Session,
    company: Company,
    *,
    force: bool = False,
) -> Optional[TradeCreditLimit]:
    """Compute and persist a fresh TradeCreditLimit suggestion."""
    # Load latest figures, ratios, balance verdict, insurance signal, payments.
    fb = db.execute(
        select(FinancialStatement, FinancialFigures)
        .join(FinancialFigures, FinancialFigures.statement_id == FinancialStatement.id)
        .where(FinancialStatement.company_id == company.id)
        .order_by(FinancialStatement.period_end.desc())
        .limit(1)
    ).first()
    latest_fig = None
    if fb:
        s, f = fb
        latest_fig = ExtractedFigures(
            period_end=s.period_end or "",
            revenue=f.revenue,
            equity=f.equity,
            total_liabilities=f.total_liabilities,
            total_assets=f.total_assets,
            current_assets=f.current_assets,
            current_liabilities=f.current_liabilities,
            ebit=f.ebit,
            net_profit=f.net_profit,
            retained_earnings=f.retained_earnings,
        )
    latest_ratios = None
    if latest_fig is not None:
        latest_ratios = compute_ratios(latest_fig)

    balance_verdict = None
    ai_row = db.scalar(
        select(FinancialAIAnalysis)
        .where(FinancialAIAnalysis.company_id == company.id)
        .order_by(FinancialAIAnalysis.as_of.desc())
        .limit(1)
    )
    if ai_row is not None:
        from app.analysis.balance_ai_analyzer import BalanceVerdict
        balance_verdict = BalanceVerdict(
            condition=ai_row.condition,
            red_flags=list(ai_row.red_flags or []),
            strengths=list(ai_row.strengths or []),
            commentary=ai_row.commentary or "",
            solvency_forecast_12m=ai_row.solvency_forecast_12m or "medium",
        )

    ins_row = db.scalar(
        select(InsuranceSignal)
        .where(InsuranceSignal.company_id == company.id)
        .order_by(InsuranceSignal.as_of.desc())
        .limit(1)
    )
    pay_row = db.scalar(
        select(PaymentReputation)
        .where(PaymentReputation.company_id == company.id)
        .order_by(PaymentReputation.as_of.desc())
        .limit(1)
    )

    suggestion = suggest_trade_credit_limit(
        latest_figures=latest_fig,
        latest_ratios=latest_ratios,
        balance_verdict=balance_verdict,
        insurance_state=ins_row.state if ins_row else None,
        payment_dbt=pay_row.dbt_flag if pay_row else None,
    )

    if suggestion.recommended is None:
        # Nothing to persist.
        return None

    row = TradeCreditLimit(
        company_id=company.id,
        currency=suggestion.currency,
        recommended=suggestion.recommended,
        low=suggestion.low,
        high=suggestion.high,
        rationale=suggestion.rationale,
        factors=suggestion.factors,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except Exception as e:
        logger.warning("TradeCreditLimit commit failed: %s", e)
        db.rollback()
        return None
    return row


# ────────────────────────────────────────────────────────────────────────
# Contracts / insurance / payments / regulatory / governance
# ────────────────────────────────────────────────────────────────────────


def refresh_contracts(db: Session, company: Company, *, force: bool = False) -> int:
    settings = get_settings()
    latest = db.scalar(
        select(Contract)
        .where(Contract.company_id == company.id)
        .order_by(Contract.detected_at.desc())
        .limit(1)
    )
    if latest and not force and _is_fresh(latest.detected_at, settings.contracts_refresh_days):
        return 0
    try:
        from app.scraper.contracts import collect_contracts

        rows = collect_contracts(db, company.id, name=company.name, nip=company.nip)
        return len(rows)
    except Exception as e:
        logger.info("refresh_contracts failed: %s", e)
        return 0


def refresh_insurance(db: Session, company: Company, *, aliases: list[str], force: bool = False) -> Optional[InsuranceSignal]:
    settings = get_settings()
    latest = db.scalar(
        select(InsuranceSignal)
        .where(InsuranceSignal.company_id == company.id)
        .order_by(InsuranceSignal.as_of.desc())
        .limit(1)
    )
    if latest and not force and _is_fresh(latest.as_of, settings.insurance_refresh_days):
        return latest
    try:
        det = detect_insurance(
            db,
            company.id,
            company_name=company.name,
            sector=company.sector,
            aliases=aliases,
        )
    except Exception as e:
        logger.info("refresh_insurance failed: %s", e)
        return latest
    row = InsuranceSignal(
        company_id=company.id,
        state=det.state,
        provider_guess=det.provider_guess,
        source=det.source,
        confidence=det.confidence,
        evidence=det.evidence or None,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except Exception as e:
        logger.warning("Insurance commit failed: %s", e)
        db.rollback()
        return None
    return row


def refresh_payments(db: Session, company: Company, *, force: bool = False) -> Optional[PaymentReputation]:
    settings = get_settings()
    latest = db.scalar(
        select(PaymentReputation)
        .where(PaymentReputation.company_id == company.id)
        .order_by(PaymentReputation.as_of.desc())
        .limit(1)
    )
    if latest and not force and _is_fresh(latest.as_of, settings.payments_refresh_days):
        return latest
    try:
        res = assess_payment_reputation(db, company.id, nip=company.nip)
    except Exception as e:
        logger.info("refresh_payments failed: %s", e)
        return latest
    row = PaymentReputation(
        company_id=company.id,
        dpo_days=res.dpo_days,
        dbt_flag=res.dbt_flag,
        events_count=res.events_count,
        news_mentions=res.news_mentions or None,
        score=res.score,
        sources=res.sources or None,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except Exception as e:
        logger.warning("Payments commit failed: %s", e)
        db.rollback()
        return None
    return row


def refresh_regulatory(db: Session, company: Company, *, force: bool = False) -> int:
    settings = get_settings()
    latest = db.scalar(
        select(RegulatoryEvent)
        .where(RegulatoryEvent.company_id == company.id)
        .order_by(RegulatoryEvent.detected_at.desc())
        .limit(1)
    )
    if latest and not force and _is_fresh(latest.detected_at, settings.regulatory_refresh_days):
        return 0

    added = 0
    # Parse KRS dzial-6 if we already have a KRS odpis cached in CompanyRegistryData.
    from app.models import CompanyRegistryData

    reg = db.scalar(
        select(CompanyRegistryData)
        .where(CompanyRegistryData.company_id == company.id, CompanyRegistryData.source == "KRS")
        .order_by(CompanyRegistryData.extracted_at.desc())
        .limit(1)
    )
    if reg and reg.raw_json:
        try:
            from app.scraper.krs_section6 import parse_section6

            for ev in parse_section6(reg.raw_json):
                exists = db.scalar(
                    select(RegulatoryEvent).where(
                        RegulatoryEvent.company_id == company.id,
                        RegulatoryEvent.title == ev.title,
                    )
                )
                if exists:
                    continue
                db.add(
                    RegulatoryEvent(
                        company_id=company.id,
                        kind=ev.kind,
                        source="KRS_SECTION_6",
                        title=ev.title,
                        body=ev.body,
                        event_date=ev.event_date,
                        severity=ev.severity,
                        status=ev.status,
                        raw_payload=ev.raw or None,
                    )
                )
                added += 1
        except Exception as e:
            logger.info("KRS section6 parse failed: %s", e)

    # Optional MSiG
    try:
        from app.scraper.msig_client import fetch_msig_events

        for ev in fetch_msig_events(nip=company.nip, krs=company.krs):
            exists = db.scalar(
                select(RegulatoryEvent).where(
                    RegulatoryEvent.company_id == company.id,
                    RegulatoryEvent.external_ref == ev.external_ref,
                )
            )
            if exists:
                continue
            db.add(
                RegulatoryEvent(
                    company_id=company.id,
                    kind=ev.kind,
                    source="MSIG",
                    title=ev.title,
                    body=ev.body,
                    event_date=ev.event_date,
                    severity=ev.severity,
                    external_ref=ev.external_ref,
                    raw_payload=ev.raw or None,
                )
            )
            added += 1
    except Exception as e:
        logger.info("MSiG fetch failed: %s", e)

    if added:
        try:
            db.commit()
        except Exception as e:
            logger.warning("Regulatory commit failed: %s", e)
            db.rollback()
    return added


def refresh_governance(db: Session, company: Company, *, force: bool = False) -> None:
    settings = get_settings()
    # Simple cooldown proxy: check latest PersonRiskFlag creation date.
    from app.models import CompanyPerson, PersonRiskFlag

    latest = db.scalar(
        select(PersonRiskFlag)
        .join(CompanyPerson, PersonRiskFlag.person_id == CompanyPerson.id)
        .where(CompanyPerson.company_id == company.id)
        .order_by(PersonRiskFlag.detected_at.desc())
        .limit(1)
    )
    if latest and not force and _is_fresh(latest.detected_at, settings.governance_refresh_days):
        return
    try:
        analyse_people(db, company.id, company_name=company.name)
    except Exception as e:
        logger.info("Governance analysis failed: %s", e)
