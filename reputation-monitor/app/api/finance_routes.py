"""REST API for the financial / commercial / governance / regulatory layers.

Endpoints exposed here read data produced by the pipeline in
``app/analysis/financial_pipeline.py``. They also offer force-refresh variants
for on-demand recomputation from the UI.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import financial_pipeline as fp
from app.analysis.contract_intensity import compute_contract_intensity
from app.database import get_db
from app.models import (
    Company,
    CompanyPerson,
    Contract,
    FinancialAIAnalysis,
    FinancialFigures,
    FinancialRatios,
    FinancialStatement,
    InsuranceSignal,
    PaymentReputation,
    PersonRiskFlag,
    RegulatoryEvent,
    TradeCreditLimit,
)

router = APIRouter()


def _require_company(db: Session, company_id: str) -> Company:
    c = db.get(Company, company_id)
    if not c:
        raise HTTPException(status_code=404, detail="Company not found")
    return c


# ─── Financials ──────────────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/financials")
def get_financials(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    rows = list(
        db.execute(
            select(FinancialStatement, FinancialFigures)
            .join(FinancialFigures, FinancialFigures.statement_id == FinancialStatement.id, isouter=True)
            .where(FinancialStatement.company_id == c.id)
            .order_by(FinancialStatement.period_end.desc())
        ).all()
    )
    ratios = list(
        db.scalars(
            select(FinancialRatios)
            .where(FinancialRatios.company_id == c.id)
            .order_by(FinancialRatios.period_end.desc())
        ).all()
    )
    ai = db.scalar(
        select(FinancialAIAnalysis)
        .where(FinancialAIAnalysis.company_id == c.id)
        .order_by(FinancialAIAnalysis.as_of.desc())
    )
    return {
        "statements": [
            {
                "id": s.id,
                "period_end": s.period_end,
                "period_type": s.period_type,
                "currency": s.currency,
                "source": s.source,
                "pdf_url": s.pdf_url,
                "extracted_at": s.extracted_at.isoformat() if s.extracted_at else None,
                "figures": _figures_dict(f) if f else None,
            }
            for s, f in rows
        ],
        "ratios": [
            {
                "period_end": r.period_end,
                "current_ratio": r.current_ratio,
                "quick_ratio": r.quick_ratio,
                "cash_ratio": r.cash_ratio,
                "debt_to_equity": r.debt_to_equity,
                "debt_to_assets": r.debt_to_assets,
                "roe": r.roe,
                "roa": r.roa,
                "net_margin": r.net_margin,
                "operating_margin": r.operating_margin,
                "asset_turnover": r.asset_turnover,
                "dpo": r.dpo,
                "dso": r.dso,
                "dio": r.dio,
                "cash_conversion_cycle": r.cash_conversion_cycle,
                "altman_z_em": r.altman_z_em,
                "maczynska_zem": r.maczynska_zem,
            }
            for r in ratios
        ],
        "ai_analysis": (
            {
                "as_of": ai.as_of.isoformat() if ai.as_of else None,
                "condition": ai.condition,
                "red_flags": ai.red_flags or [],
                "strengths": ai.strengths or [],
                "short_term_risks": ai.short_term_risks or [],
                "long_term_risks": ai.long_term_risks or [],
                "commentary": ai.commentary,
                "solvency_forecast_12m": ai.solvency_forecast_12m,
                "years_covered": ai.years_covered or [],
            }
            if ai
            else None
        ),
    }


def _figures_dict(f: FinancialFigures) -> Dict[str, Any]:
    return {
        "revenue": f.revenue,
        "ebit": f.ebit,
        "ebitda": f.ebitda,
        "net_profit": f.net_profit,
        "total_assets": f.total_assets,
        "current_assets": f.current_assets,
        "non_current_assets": f.non_current_assets,
        "cash": f.cash,
        "inventory": f.inventory,
        "receivables": f.receivables,
        "total_liabilities": f.total_liabilities,
        "current_liabilities": f.current_liabilities,
        "non_current_liabilities": f.non_current_liabilities,
        "trade_payables": f.trade_payables,
        "equity": f.equity,
        "retained_earnings": f.retained_earnings,
        "cash_from_operations": f.cash_from_operations,
        "capex": f.capex,
    }


@router.post("/api/companies/{company_id}/financials/refresh")
def refresh_financials(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    stmts = fp.refresh_financial_statements(db, c, force=True)
    fp.refresh_financial_ratios(db, c)
    ai = fp.refresh_balance_ai(db, c, force=True)
    return {
        "statements_count": len(stmts),
        "ai_condition": ai.condition if ai else None,
    }


# ─── Trade credit limit ──────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/trade-credit-limit")
def get_trade_credit_limit(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    row = db.scalar(
        select(TradeCreditLimit)
        .where(TradeCreditLimit.company_id == c.id)
        .order_by(TradeCreditLimit.as_of.desc())
    )
    if not row:
        return {"limit": None}
    return {
        "limit": {
            "as_of": row.as_of.isoformat() if row.as_of else None,
            "currency": row.currency,
            "recommended": row.recommended,
            "low": row.low,
            "high": row.high,
            "rationale": row.rationale,
            "factors": row.factors,
        }
    }


@router.post("/api/companies/{company_id}/trade-credit-limit/refresh")
def refresh_trade_credit_limit_endpoint(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    row = fp.refresh_trade_credit_limit(db, c, force=True)
    if not row:
        return {"limit": None}
    return {
        "limit": {
            "currency": row.currency,
            "recommended": row.recommended,
            "rationale": row.rationale,
        }
    }


# ─── Contracts ───────────────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/contracts")
def get_contracts(
    company_id: str,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    rows = list(
        db.scalars(
            select(Contract)
            .where(Contract.company_id == c.id)
            .order_by(Contract.detected_at.desc())
            .limit(limit)
        ).all()
    )
    intensity = compute_contract_intensity(rows)
    return {
        "items": [
            {
                "id": r.id,
                "source": r.source,
                "counterparty": r.counterparty,
                "title": r.title,
                "value_pln": r.value_pln,
                "currency": r.currency,
                "award_date": r.award_date.isoformat() if r.award_date else None,
                "end_date": r.end_date.isoformat() if r.end_date else None,
                "status": r.status,
                "url": r.url,
            }
            for r in rows
        ],
        "intensity": {
            "active_count": intensity.active_count,
            "last_12m_count": intensity.last_12m_count,
            "prev_12m_count": intensity.prev_12m_count,
            "last_12m_value": intensity.last_12m_value,
            "prev_12m_value": intensity.prev_12m_value,
            "yoy_count_change": intensity.yoy_count_change,
            "yoy_value_change": intensity.yoy_value_change,
            "counterparty_hhi": intensity.counterparty_hhi,
            "public_share": intensity.public_share,
            "risk_score": intensity.risk_score,
        },
    }


@router.post("/api/companies/{company_id}/contracts/refresh")
def refresh_contracts_endpoint(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    added = fp.refresh_contracts(db, c, force=True)
    return {"new_contracts": added}


# ─── Payment reputation ──────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/payment-reputation")
def get_payment_reputation(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    row = db.scalar(
        select(PaymentReputation)
        .where(PaymentReputation.company_id == c.id)
        .order_by(PaymentReputation.as_of.desc())
    )
    if not row:
        return {"payment": None}
    return {
        "payment": {
            "as_of": row.as_of.isoformat() if row.as_of else None,
            "dpo_days": row.dpo_days,
            "dbt_flag": row.dbt_flag,
            "events_count": row.events_count,
            "news_mentions": row.news_mentions or [],
            "score": row.score,
            "sources": row.sources,
        }
    }


@router.post("/api/companies/{company_id}/payment-reputation/refresh")
def refresh_payment_reputation_endpoint(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    row = fp.refresh_payments(db, c, force=True)
    return {"dbt_flag": row.dbt_flag if row else None}


# ─── Insurance ───────────────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/insurance")
def get_insurance(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    row = db.scalar(
        select(InsuranceSignal)
        .where(InsuranceSignal.company_id == c.id)
        .order_by(InsuranceSignal.as_of.desc())
    )
    if not row:
        return {"insurance": None}
    return {
        "insurance": {
            "as_of": row.as_of.isoformat() if row.as_of else None,
            "state": row.state,
            "provider_guess": row.provider_guess,
            "source": row.source,
            "confidence": row.confidence,
            "evidence": row.evidence or [],
        }
    }


@router.post("/api/companies/{company_id}/insurance/refresh")
def refresh_insurance_endpoint(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    row = fp.refresh_insurance(db, c, aliases=list(c.aliases or []), force=True)
    return {"state": row.state if row else None}


# ─── Governance ──────────────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/governance")
def get_governance(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    persons = list(
        db.scalars(
            select(CompanyPerson)
            .where(CompanyPerson.company_id == c.id)
            .order_by(CompanyPerson.is_active.desc(), CompanyPerson.full_name)
        ).all()
    )
    out: List[Dict[str, Any]] = []
    for p in persons:
        flags = list(
            db.scalars(
                select(PersonRiskFlag)
                .where(PersonRiskFlag.person_id == p.id)
                .order_by(PersonRiskFlag.severity.desc())
            ).all()
        )
        out.append(
            {
                "id": p.id,
                "full_name": p.full_name,
                "role": p.role,
                "start_date": p.start_date,
                "end_date": p.end_date,
                "is_active": p.is_active,
                "flags": [
                    {
                        "kind": fl.kind,
                        "severity": fl.severity,
                        "other_company_name": fl.other_company_name,
                        "other_company_krs": fl.other_company_krs,
                        "notes": fl.notes,
                        "evidence_url": fl.evidence_url,
                        "detected_at": fl.detected_at.isoformat() if fl.detected_at else None,
                    }
                    for fl in flags
                ],
            }
        )
    total_flags = sum(len(p["flags"]) for p in out)
    return {"persons": out, "total_flags": total_flags}


@router.post("/api/companies/{company_id}/governance/refresh")
def refresh_governance_endpoint(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    from sqlalchemy import func

    c = _require_company(db, company_id)
    fp.refresh_governance(db, c, force=True)
    flags = int(
        db.scalar(
            select(func.count(PersonRiskFlag.id))
            .join(CompanyPerson, PersonRiskFlag.person_id == CompanyPerson.id)
            .where(CompanyPerson.company_id == c.id)
        )
        or 0
    )
    return {"flags": flags}


# ─── Regulatory events ───────────────────────────────────────────────────

@router.get("/api/companies/{company_id}/regulatory")
def get_regulatory(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    rows = list(
        db.scalars(
            select(RegulatoryEvent)
            .where(RegulatoryEvent.company_id == c.id)
            .order_by(RegulatoryEvent.detected_at.desc())
        ).all()
    )
    return {
        "events": [
            {
                "id": r.id,
                "kind": r.kind,
                "source": r.source,
                "title": r.title,
                "body": r.body,
                "event_date": r.event_date.isoformat() if r.event_date else None,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
                "severity": r.severity,
                "status": r.status,
                "external_ref": r.external_ref,
            }
            for r in rows
        ]
    }


@router.post("/api/companies/{company_id}/regulatory/refresh")
def refresh_regulatory_endpoint(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    c = _require_company(db, company_id)
    added = fp.refresh_regulatory(db, c, force=True)
    return {"new_events": added}


# ─── Unified profile bundle (convenience for the UI) ─────────────────────

@router.get("/api/companies/{company_id}/profile-bundle")
def get_profile_bundle(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """One-shot aggregate used by the new company profile tabs."""
    c = _require_company(db, company_id)
    return {
        "financials": get_financials(company_id, db=db),
        "trade_credit_limit": get_trade_credit_limit(company_id, db=db).get("limit"),
        "contracts": get_contracts(company_id, db=db),
        "payment_reputation": get_payment_reputation(company_id, db=db).get("payment"),
        "insurance": get_insurance(company_id, db=db).get("insurance"),
        "governance": get_governance(company_id, db=db),
        "regulatory": get_regulatory(company_id, db=db),
    }
