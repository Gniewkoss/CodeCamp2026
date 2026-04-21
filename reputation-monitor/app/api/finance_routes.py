"""REST API for the financial / commercial / governance / regulatory layers.

Endpoints exposed here read data produced by the pipeline in
``app/analysis/financial_pipeline.py``. They also offer force-refresh variants
for on-demand recomputation from the UI.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

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
    try:
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("get_financials: DB failure for company_id=%s", company_id)
        return {
            "statements": [], "ratios": [], "ai_analysis": None,
            "status": "error",
            "message": f"Błąd bazy danych podczas wczytywania finansów: {exc}",
        }
    logger.info(
        "get_financials: company=%s statements=%d ratios=%d ai=%s",
        company_id, len(rows), len(ratios), bool(ai),
    )
    status = "ok" if rows or ratios or ai else "empty"
    if status == "empty":
        message = (
            "Brak danych finansowych w bazie. "
            "Użyj przycisku „Odśwież finanse”, aby pobrać sprawozdania z KRS RDF."
        )
    else:
        message = None
    return {
        "status": status,
        "message": message,
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
    logger.info("refresh_financials: starting for company=%s (%s)", c.id, c.name)
    try:
        stmts = fp.refresh_financial_statements(db, c, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("refresh_financials: refresh_financial_statements failed")
        return {
            "status": "error",
            "stage": "statements",
            "statements_count": 0,
            "ai_condition": None,
            "message": (
                "Nie udało się pobrać sprawozdań z KRS RDF. "
                f"Powód: {exc}"
            ),
        }
    try:
        fp.refresh_financial_ratios(db, c)
    except Exception as exc:  # noqa: BLE001
        logger.exception("refresh_financials: refresh_financial_ratios failed")
        return {
            "status": "error",
            "stage": "ratios",
            "statements_count": len(stmts),
            "ai_condition": None,
            "message": f"Sprawozdania pobrane, ale obliczenia wskaźników się nie powiodły: {exc}",
        }
    try:
        ai = fp.refresh_balance_ai(db, c, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("refresh_financials: refresh_balance_ai failed")
        return {
            "status": "partial",
            "stage": "ai",
            "statements_count": len(stmts),
            "ai_condition": None,
            "message": (
                "Sprawozdania i wskaźniki gotowe, ale analiza AI się nie powiodła. "
                f"Powód: {exc}"
            ),
        }
    if not stmts:
        return {
            "status": "empty",
            "statements_count": 0,
            "ai_condition": None,
            "message": (
                "Nie znaleziono sprawozdań finansowych w KRS RDF. "
                "Spółka może być zwolniona z obowiązku publikacji albo nie złożyła "
                "jeszcze ostatniego rocznika."
            ),
        }
    return {
        "status": "ok",
        "statements_count": len(stmts),
        "ai_condition": ai.condition if ai else None,
        "message": None,
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
            # UI expects ``prev_12m_count`` — underlying dataclass uses
            # ``prior_12m_count``. Keep the name mapping here so we don't have
            # to migrate the template.
            "prev_12m_count": intensity.prior_12m_count,
            "last_12m_value": intensity.last_12m_value_pln,
            "prev_12m_value": intensity.prior_12m_value_pln,
            "yoy_count_change": intensity.count_yoy,
            "yoy_value_change": intensity.value_yoy,
            "counterparty_hhi": intensity.hhi,
            "public_share": intensity.public_share,
            "risk_score": intensity.score,
            "red_flags": intensity.red_flags or [],
            "positives": intensity.positives or [],
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
                "source": getattr(p, "source", None) or "KRS",
                "confidence": getattr(p, "confidence", None),
                "bio_notes": getattr(p, "notes", None),
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
    masked_count = sum(
        1 for p in out if p.get("source") in ("KRS_masked", "KRS")
        and "*" in (p.get("full_name") or "")
    )
    verified_count = sum(1 for p in out if p.get("source") == "krs+llm")
    ai_only_count = sum(
        1 for p in out if p.get("source") in ("llm_public", "claude_knowledge")
    )
    meta: Dict[str, Any] = {
        "total": len(out),
        "masked_by_krs": masked_count,
        "resolved_by_ai": verified_count + ai_only_count,
        "verified_by_krs_mask": verified_count,
    }
    # Compose a notice that tells the user *exactly* how much of the list
    # they can trust as "really sitting on that seat right now".
    if verified_count and masked_count:
        meta["notice"] = (
            f"Zweryfikowane z KRS: {verified_count} osób (pierwsza litera i długość "
            f"imienia zgodne z oficjalnym wpisem). Pozostałe {masked_count} pozycji "
            "zachowują zamaskowane dane z KRS — AI nie zna publicznie ich nazwisk."
        )
    elif verified_count and ai_only_count:
        meta["notice"] = (
            f"Zweryfikowane z KRS: {verified_count}. Dodatkowo {ai_only_count} "
            "osób z publicznej wiedzy AI (bez weryfikacji w rejestrze)."
        )
    elif verified_count:
        meta["notice"] = (
            f"Wszystkie {verified_count} osób zweryfikowane: pierwsza litera i "
            "długość imienia/nazwiska zgodne z oficjalnym wpisem w KRS."
        )
    elif masked_count and not ai_only_count:
        meta["notice"] = (
            "KRS REST API zwraca zamaskowane imiona i nazwiska (RODO). "
            "Użyj „Sprawdź historię”, żeby AI spróbował dopasować nazwiska do "
            "maski z KRS (pierwsza litera + długość)."
        )
    elif ai_only_count:
        meta["notice"] = (
            f"{ai_only_count} osób z publicznej wiedzy AI (spółka bez numeru KRS — "
            "brak maski do weryfikacji)."
        )
    elif not out:
        if not c.krs:
            meta["notice"] = (
                "Brak zarządu — spółka nie ma przypisanego numeru KRS. "
                "Dodaj KRS w profilu albo użyj „Sprawdź historię”, aby AI "
                "spróbował rozwiązać skład z publicznej wiedzy."
            )
        else:
            meta["notice"] = (
                "Brak zarządu — KRS REST API nic nie zwróciło, a AI nie ma pewnej "
                "publicznej wiedzy o składzie zarządu tej spółki. "
                "Kliknij „Sprawdź historię”, żeby ponowić próbę."
            )
    return {"persons": out, "total_flags": total_flags, "meta": meta}


@router.post("/api/companies/{company_id}/governance/refresh")
def refresh_governance_endpoint(company_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    from sqlalchemy import func

    from app.services.registry_sync import sync_krs_for_company

    c = _require_company(db, company_id)
    # Re-sync KRS first. This re-runs the board resolver (Claude) if KRS
    # returns only masked rows, so "Sprawdź historię" actually populates
    # human-readable names on repeat clicks.
    krs_info: Dict[str, Any] = {}
    try:
        krs_info = sync_krs_for_company(db, c.id)
    except Exception as exc:  # noqa: BLE001
        logger.info("KRS re-sync during governance refresh failed: %s", exc)
        krs_info = {"ok": False, "error": str(exc)[:200]}

    try:
        fp.refresh_governance(db, c, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.info("Governance analysis failed: %s", exc)
        krs_info.setdefault("error", str(exc)[:200])

    flags = int(
        db.scalar(
            select(func.count(PersonRiskFlag.id))
            .join(CompanyPerson, PersonRiskFlag.person_id == CompanyPerson.id)
            .where(CompanyPerson.company_id == c.id)
        )
        or 0
    )
    persons_count = int(
        db.scalar(
            select(func.count(CompanyPerson.id)).where(CompanyPerson.company_id == c.id)
        )
        or 0
    )

    # Translate the various "nothing came back" situations into a single
    # human-readable status that the UI can surface verbatim in a toast.
    ok = persons_count > 0
    raw_err = (krs_info or {}).get("error") or ""
    err_lower = raw_err.lower()
    # Map provider-specific billing/quota failures to a single, provider-agnostic
    # message so the UI never hard-codes "Anthropic" when we're running on GPT
    # (or vice versa). The live provider comes from ``active_provider()``.
    is_billing_error = (
        "credit balance is too low" in err_lower  # Anthropic
        or "insufficient_quota" in err_lower  # OpenAI
        or "rate_limit_exceeded" in err_lower
        or "you exceeded your current quota" in err_lower
    )
    if is_billing_error:
        from app.llm import active_provider
        provider_label = {"openai": "OpenAI", "anthropic": "Anthropic"}.get(
            active_provider(), "LLM"
        )
        message = (
            f"Klucz {provider_label} wyczerpał limit / kredyty — doładuj konto "
            "albo przełącz ``LLM_PROVIDER`` w pliku .env, żeby AI odzyskał "
            "pełne imiona i nazwiska z publicznej wiedzy."
        )
    elif not c.krs and persons_count == 0:
        message = (
            "Spółka nie ma przypisanego numeru KRS — dodaj go w profilu "
            "albo poczekaj aż AI rozwiąże skład zarządu z publicznych danych."
        )
    elif persons_count == 0 and raw_err:
        message = f"Nie udało się pobrać zarządu: {raw_err}"
    elif persons_count == 0:
        message = "KRS nie zwrócił żadnych osób i AI nie zna publicznie składu zarządu."
    else:
        resolved = int((krs_info or {}).get("resolved_from_ai") or 0)
        verified = int((krs_info or {}).get("verified_from_krs") or 0)
        masked = int((krs_info or {}).get("krs_masked") or 0)
        bits = [f"{persons_count} osób w zarządzie/RN"]
        if verified:
            bits.append(f"✓ {verified} zweryfikowane z KRS")
        if resolved and resolved > verified:
            bits.append(f"{resolved - verified} z AI (niezweryfikowane)")
        unresolved = max(0, masked - verified) if masked else 0
        if unresolved:
            bits.append(f"{unresolved} pozostaje zamaskowane")
        if flags:
            bits.append(f"{flags} flag ryzyka")
        message = ", ".join(bits)

    return {
        "ok": ok,
        "message": message,
        "flags": flags,
        "persons": persons_count,
        "krs": krs_info,
    }


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
    """One-shot aggregate used by the new company profile tabs.

    Every sub-section is wrapped so a single failure (e.g. a malformed
    row, a schema regression, a broken dataclass) does not blow up the
    whole response and leave the UI with an empty profile. Failed
    sections are reported under ``_errors`` instead.
    """
    c = _require_company(db, company_id)
    out: Dict[str, Any] = {
        "financials": None,
        "trade_credit_limit": None,
        "contracts": None,
        "payment_reputation": None,
        "insurance": None,
        "governance": None,
        "regulatory": None,
        "_errors": {},
    }

    def _safe(name: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — we really want to eat everything
            logger.exception("profile-bundle: %s failed for %s: %s", name, company_id, exc)
            out["_errors"][name] = str(exc)[:300]
            return None

    out["financials"] = _safe("financials", lambda: get_financials(company_id, db=db))
    tcl = _safe("trade_credit_limit", lambda: get_trade_credit_limit(company_id, db=db))
    out["trade_credit_limit"] = (tcl or {}).get("limit") if tcl else None
    out["contracts"] = _safe("contracts", lambda: get_contracts(company_id, db=db))
    pay = _safe("payment_reputation", lambda: get_payment_reputation(company_id, db=db))
    out["payment_reputation"] = (pay or {}).get("payment") if pay else None
    ins = _safe("insurance", lambda: get_insurance(company_id, db=db))
    out["insurance"] = (ins or {}).get("insurance") if ins else None
    out["governance"] = _safe("governance", lambda: get_governance(company_id, db=db))
    out["regulatory"] = _safe("regulatory", lambda: get_regulatory(company_id, db=db))
    return out
