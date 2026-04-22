"""Persist KRS / CEIDG v2 responses into company_registry_data + company_persons."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.analysis.board_resolver import resolve_board
from app.models import Company, CompanyPerson, CompanyRegistryData
from app.config import get_settings
from app.scraper.ceidg_v2 import fetch_ceidg_firma_v2
from app.scraper.registry import apply_registry_record, gus_lookup
from app.scraper.krs_client import (
    PersonSignature,
    extract_krs_highlights,
    extract_person_signatures,
    extract_persons_from_krs_blob,
    fetch_krs_odpis_json,
)

logger = logging.getLogger(__name__)


_re = __import__("re")
_MASK_RE = _re.compile(r"\*{2,}")
# Dotted-initials variant ("J. K." / "A.B."). We accept either Polish or
# ASCII letters — some press releases strip diacritics.
_INITIALS_RE = _re.compile(
    r"^[A-ZĄĆĘŁŃÓŚŹŻ]\.\s*[A-ZĄĆĘŁŃÓŚŹŻ]\.?$", _re.IGNORECASE
)


def _is_masked(name: str | None) -> bool:
    """A name is considered masked if it contains the GDPR star mask OR if
    it's a bare ``"J. K."`` initials pair. In both cases we still want to
    persist the person — they're a real seat on the board, just without a
    public full name."""
    if not name:
        return False
    return bool(_MASK_RE.search(name) or _INITIALS_RE.match(name.strip()))


def _add_registry_row(db: Session, company_id: str, source: str, raw: dict[str, Any]) -> None:
    db.add(
        CompanyRegistryData(
            company_id=company_id,
            source=source,
            raw_json=raw,
            extracted_at=datetime.now(timezone.utc),
        )
    )


def _resolve_and_persist_board(
    db: Session,
    company: Company,
    signatures: list[PersonSignature] | None = None,
) -> tuple[int, int, str, str, set[int]]:
    """Ask the LLM for the publicly-known board and persist matching rows.

    When ``signatures`` are supplied (extracted from the masked KRS
    response), the resolver returns names constrained by the mask —
    first-letter + length must match, otherwise the suggestion is
    dropped. Rows where the mask matches are persisted with
    ``source="krs+llm"`` (verified), rows without a mask get
    ``source="llm_public"`` (unverified).

    Returns ``(added_count, verified_count, confidence, identity_note,
    resolved_seat_ids)``. Caller is responsible for ``db.commit()``.
    """
    resolution = resolve_board(
        company_name=company.name,
        sector=company.sector,
        krs=company.krs,
        nip=company.nip,
        signatures=signatures,
    )
    added = 0
    verified_count = 0
    resolved_seat_ids: set[int] = set()
    base_confidence = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(
        resolution.confidence, 0.5
    )
    for rp in resolution.persons:
        row_conf = 0.95 if rp.verified else base_confidence
        source = "krs+llm" if rp.verified else "llm_public"
        if rp.verified:
            verified_count += 1
        if rp.signature_idx is not None:
            resolved_seat_ids.add(rp.signature_idx)
        db.add(
            CompanyPerson(
                company_id=company.id,
                full_name=rp.full_name[:512],
                role=(rp.role or "—")[:128],
                start_date=rp.start_date,
                is_active=True,
                source=source,
                confidence=row_conf,
                notes=rp.notes,
            )
        )
        added += 1
    return added, verified_count, resolution.confidence, resolution.identity_note, resolved_seat_ids


def sync_krs_for_company(db: Session, company_id: str) -> dict[str, Any]:
    """Synchronise board/management for a company.

    Resolution waterfall:

    1. Fetch the KRS REST odpis. It returns the organ structure verbatim
       **but masks first+last names** (``"F*****"``) — what we do keep is
       the first letter, the exact length of the real name and the full
       role (``"PREZES ZARZĄDU"``).
    2. If at least one member is masked, we pass those signatures to the
       GPT-4o board resolver. The resolver must return names that match
       the mask (letter + length); we re-check every suggestion in
       Python. Matched names get persisted as ``source="krs+llm"`` and
       trusted as if they came from KRS itself.
    3. For companies added **without** a KRS number (quick-lookup), we
       fall back to the unconstrained resolver.
    4. If everything fails we still persist the masked KRS roster so the
       UI at least shows the roles + seat count.
    """
    company = db.get(Company, company_id)
    if not company:
        return {"ok": False, "error": "no company"}

    persons_raw: list[dict[str, Any]] = []
    signatures: list[PersonSignature] = []
    masked_count = 0
    krs_error: str | None = None

    if company.krs:
        try:
            data = fetch_krs_odpis_json(company.krs)
            _add_registry_row(db, company_id, "KRS", data)
            highlights = extract_krs_highlights(data)
            if highlights.get("legal_form") and not company.legal_form:
                company.legal_form = str(highlights["legal_form"])[:128]
            if highlights.get("registration_date") and not company.registration_date:
                company.registration_date = str(highlights["registration_date"])[:32]
            persons_raw = extract_persons_from_krs_blob(data)
            masked_count = sum(1 for p in persons_raw if _is_masked(p.get("full_name")))
            signatures = extract_person_signatures(data)
        except Exception as e:
            logger.warning("KRS fetch failed for %s (%s): %s", company.name, company.krs, e)
            krs_error = str(e)[:200]

    all_masked = bool(persons_raw) and masked_count == len(persons_raw)
    # We call the resolver whenever the KRS names are masked OR when the
    # KRS returned nothing at all (e.g. company added without a KRS number).
    need_ai = all_masked or not persons_raw

    db.execute(delete(CompanyPerson).where(CompanyPerson.company_id == company_id))
    resolved_persons = 0
    verified_persons = 0
    resolution_confidence = ""
    resolution_note = ""

    resolved_seat_ids: set[int] = set()
    if need_ai:
        (
            resolved_persons,
            verified_persons,
            resolution_confidence,
            resolution_note,
            resolved_seat_ids,
        ) = _resolve_and_persist_board(db, company, signatures=signatures or None)
        logger.info(
            "Board resolver for %s: +%d persons (%d verified from %d KRS seats, confidence=%s) note=%s",
            company.name,
            resolved_persons,
            verified_persons,
            len(signatures),
            resolution_confidence,
            resolution_note[:120],
        )

    # For every KRS seat the LLM couldn't confidently name, still add a
    # masked placeholder so the UI displays the complete roster (count +
    # roles). Without this the "7 of 24 persons" view becomes a liar.
    # NOTE: ``resolved_seat_ids`` uses the **global** enumeration we hand
    # to the LLM (1..N across all organs), which is NOT the same as
    # ``PersonSignature.position_idx`` (1-based within an organ). We must
    # enumerate in the same order as the prompt.
    if signatures:
        for i, sig in enumerate(signatures, start=1):
            if i in resolved_seat_ids:
                continue
            db.add(
                CompanyPerson(
                    company_id=company_id,
                    full_name=sig.display[:512] or "—",
                    role=sig.role[:128] or "—",
                    is_active=True,
                    source="KRS_masked",
                    notes=(
                        "Nie rozpoznano przez AI. KRS maskuje imiona i nazwiska "
                        "(RODO); widoczne są pierwsza litera i liczba znaków "
                        f"({len(sig.first_name_mask)}+{len(sig.last_name_mask)})."
                    ),
                )
            )
    elif resolved_persons == 0 and persons_raw:
        # No signatures at all (older code path / parser edge case) — fall
        # back to the heuristic person list so at least roles are visible.
        for p in persons_raw:
            db.add(
                CompanyPerson(
                    company_id=company_id,
                    full_name=p["full_name"][:512],
                    role=(p.get("role") or "—")[:128],
                    is_active=True,
                    source="KRS",
                    notes=(
                        "KRS maskuje imiona i nazwiska (RODO). "
                        "Pełne dane tylko w Odpisie Pełnym."
                    ),
                )
            )

    db.commit()

    # UI headline count: one row per KRS seat (so "24 osób" matches what
    # iMSiG / official sources would show), plus any rows we've got from
    # the unconstrained path when there was no KRS at all.
    total_persons = len(signatures) if signatures else (resolved_persons or len(persons_raw))
    return {
        "ok": total_persons > 0,
        "persons": total_persons,
        "resolved_from_ai": resolved_persons,
        "verified_from_krs": verified_persons,
        "krs_signatures": len(signatures),
        "krs_masked": masked_count,
        "krs_total": len(persons_raw),
        "krs_error": krs_error,
        "resolution_confidence": resolution_confidence,
        "resolution_note": resolution_note,
        "error": (
            None
            if total_persons > 0
            else (
                krs_error
                or resolution_note
                or "Brak danych w KRS oraz w publicznej wiedzy LLM."
            )
        ),
    }


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


def sync_gus_bir_for_company(db: Session, company_id: str) -> dict[str, Any]:
    """REGON / GUS BIR — PKD, forma prawna, pełniejszy adres, raport BIR11 (prod)."""
    settings = get_settings()
    if not settings.gus_bir_enabled or not (settings.gus_bir_api_key or "").strip():
        return {"ok": False, "skipped": True, "reason": "GUS_BIR disabled or no GUS_BIR_API_KEY"}

    company = db.get(Company, company_id)
    if not company:
        return {"ok": False, "error": "no company"}
    if not (company.nip or company.regon or company.krs):
        return {"ok": False, "skipped": True, "reason": "no nip/regon/krs for GUS lookup"}

    rec = gus_lookup(
        nip=company.nip,
        regon=company.regon,
        krs=company.krs,
    )
    if not rec:
        return {"ok": False, "error": "GUS BIR returned no data (check NIP/REGON or key)"}

    apply_registry_record(company, rec)
    _add_registry_row(db, company_id, "GUS_BIR", rec.raw or {"gus": True})
    db.commit()
    return {"ok": True, "sources": rec.sources, "name": rec.name, "pkd": rec.pkd_primary}


def sync_all_registries(db: Session, company_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if db.get(Company, company_id):
        out["krs"] = sync_krs_for_company(db, company_id)
        out["gus_bir"] = sync_gus_bir_for_company(db, company_id)
        out["ceidg_v2"] = sync_ceidg_v2_for_company(db, company_id)
    return out
