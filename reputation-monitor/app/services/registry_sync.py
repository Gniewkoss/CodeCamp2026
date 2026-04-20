"""Persist KRS / CEIDG v2 responses into company_registry_data + company_persons."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Company, CompanyPerson, CompanyRegistryData
from app.scraper.ceidg_v2 import fetch_ceidg_firma_v2
from app.scraper.krs_client import extract_krs_highlights, extract_persons_from_krs_blob, fetch_krs_odpis_json

logger = logging.getLogger(__name__)


def _add_registry_row(db: Session, company_id: str, source: str, raw: dict[str, Any]) -> None:
    db.add(
        CompanyRegistryData(
            company_id=company_id,
            source=source,
            raw_json=raw,
            extracted_at=datetime.now(timezone.utc),
        )
    )


def sync_krs_for_company(db: Session, company_id: str) -> dict[str, Any]:
    company = db.get(Company, company_id)
    if not company or not company.krs:
        return {"ok": False, "error": "no KRS"}
    try:
        data = fetch_krs_odpis_json(company.krs)
    except Exception as e:
        logger.warning("KRS fetch failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}
    _add_registry_row(db, company_id, "KRS", data)
    highlights = extract_krs_highlights(data)
    if highlights.get("legal_form") and not company.legal_form:
        company.legal_form = str(highlights["legal_form"])[:128]
    if highlights.get("registration_date") and not company.registration_date:
        company.registration_date = str(highlights["registration_date"])[:32]
    persons = extract_persons_from_krs_blob(data)
    db.execute(delete(CompanyPerson).where(CompanyPerson.company_id == company_id))
    for p in persons:
        db.add(
            CompanyPerson(
                company_id=company_id,
                full_name=p["full_name"][:512],
                role=(p.get("role") or "—")[:128],
                is_active=True,
            )
        )
    db.commit()
    return {"ok": True, "persons": len(persons)}


def sync_ceidg_v2_for_company(db: Session, company_id: str) -> dict[str, Any]:
    company = db.get(Company, company_id)
    if not company or not company.nip:
        return {"ok": False, "error": "no NIP"}
    data = fetch_ceidg_firma_v2(nip=company.nip)
    if not data:
        return {"ok": False, "error": "CEIDG v2 empty or unavailable"}
    payload = data if isinstance(data, dict) else {"payload": data}
    _add_registry_row(db, company_id, "CEIDG_V2", payload)
    firma = payload.get("firma") or payload
    if isinstance(firma, list) and firma:
        firma = firma[0]
    if isinstance(firma, dict):
        owner = firma.get("wlasciciel") or {}
        im = owner.get("imie")
        nz = owner.get("nazwisko")
        if im or nz:
            name = f"{im or ''} {nz or ''}".strip()
            exists = db.scalar(
                select(CompanyPerson).where(
                    CompanyPerson.company_id == company_id,
                    CompanyPerson.full_name == name,
                )
            )
            if not exists:
                db.add(
                    CompanyPerson(
                        company_id=company_id,
                        full_name=name[:512],
                        role="właściciel (CEIDG)",
                        is_active=True,
                    )
                )
    db.commit()
    return {"ok": True}


def sync_all_registries(db: Session, company_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if db.get(Company, company_id):
        out["krs"] = sync_krs_for_company(db, company_id)
        out["ceidg_v2"] = sync_ceidg_v2_for_company(db, company_id)
    return out
