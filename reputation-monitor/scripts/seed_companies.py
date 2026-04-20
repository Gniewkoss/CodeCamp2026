"""Seed demo companies + sample risk ledger events."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.analysis.event_types import EVENT_TYPES
from app.database import SessionLocal, init_db
from app.models import Company, RiskEvent
from app.scoring.calculator import recalculate_and_persist

SAMPLES = [
    {
        "name": "PKN Orlen SA",
        "aliases": ["PKN Orlen", "Orlen", "Orlen SA", "PKN"],
        "nip": "7740001454",
        "krs": "0000028860",
        "ticker": "PKN",
        "sector": "Paliwa / Energia",
    },
    {
        "name": "KGHM Polska Miedź SA",
        "aliases": ["KGHM", "KGHM Polska Miedź", "Polska Miedź"],
        "nip": "6920000213",
        "krs": "0000014243",
        "ticker": "KGH",
        "sector": "Surowce",
    },
    {
        "name": "mBank SA",
        "aliases": ["mBank", "Bank mBank"],
        "nip": "5260215088",
        "krs": "0000010272",
        "ticker": "MBK",
        "sector": "Bankowość",
    },
    {
        "name": "CD Projekt SA",
        "aliases": ["CD Projekt", "CDPR", "CD Projekt Red"],
        "nip": "7342867148",
        "krs": "0000006862",
        "ticker": "CDR",
        "sector": "Gaming / Technologie",
    },
    {
        "name": "LPP SA",
        "aliases": ["LPP", "Reserved", "Cropp", "Sinsay", "House"],
        "nip": "5860208581",
        "krs": "0000028577",
        "ticker": "LPP",
        "sector": "Handel detaliczny",
    },
    {
        "name": "Allegro.eu SA",
        "aliases": ["Allegro", "Allegro.pl"],
        "nip": "5252625944",
        "krs": "0000635012",
        "ticker": "ALE",
        "sector": "E-commerce",
    },
    {
        "name": "Santander Bank Polska SA",
        "aliases": ["Santander", "BZWBK", "Santander Polska"],
        "nip": "8960005673",
        "krs": "0000008723",
        "ticker": "SPL",
        "sector": "Bankowość",
    },
    {
        "name": "PGE Polska Grupa Energetyczna SA",
        "aliases": ["PGE", "Polska Grupa Energetyczna"],
        "nip": "5260250541",
        "krs": "0000059307",
        "ticker": "PGE",
        "sector": "Energetyka",
    },
]


def _seed_events_for_companies(db) -> int:
    existing = db.scalar(select(func.count()).select_from(RiskEvent)) or 0
    if existing > 0:
        return 0
    companies = list(db.scalars(select(Company)).all())
    if not companies:
        return 0
    now = datetime.now(timezone.utc)
    added = 0
    templates = [
        ("regulatory_fine", "active", None, "Grzywna UOKiK — postępowanie w toku", "W toku postępowanie administracyjne."),
        ("investigation_opened", "resolved", 120, "Prokuratura wszędzie czynności wyjaśniające", "Śledztwo zakończone bez zarzutów."),
        ("negative_media_spike", "mitigated", 200, "Wzrost negatywnych doniesień medialnych", "Komunikat spółki ustosunkował się do zarzutów."),
        ("ceo_resignation_normal", "historical", 400, "Zmiana na stanowisku prezesa", "Rotacja zgodnie z planem."),
        ("tax_arrears", "active", None, "Zaległości podatkowe — monitoring", "Wykaz zaległości w ograniczonej wysokości."),
    ]
    for i, c in enumerate(companies):
        for j, (etype, status, days_ago, title, desc) in enumerate(templates):
            detected = now - timedelta(days=(days_ago or 0) + j * 3)
            ev_date = detected - timedelta(days=2)
            resolved_at = None
            if status == "resolved":
                resolved_at = detected + timedelta(days=30)
            if status == "mitigated":
                resolved_at = detected + timedelta(days=45)
            ev = RiskEvent(
                company_id=c.id,
                event_type=etype,
                title=title[:512],
                description=desc,
                severity=float(EVENT_TYPES.get(etype, 0.5)),
                source_url="https://example.com/demo-article",
                source_name="demo_seed",
                detected_at=detected,
                event_date=ev_date,
                status=status,
                resolved_at=resolved_at,
                resolution_note="Zamknięto w symulacji seed." if resolved_at else None,
                related_person="Jan Kowalski" if j % 2 == 0 else None,
            )
            db.add(ev)
            added += 1
        if i == 0:
            db.add(
                RiskEvent(
                    company_id=c.id,
                    event_type="sanctions_match_company",
                    title="[TEST] Symulacja trafienia na listę sankcyjną",
                    description="Sztuczne zdarzenie testowe — nie oznacza rzeczywistego wpisu.",
                    severity=float(EVENT_TYPES["sanctions_match_company"]),
                    source_url="https://www.gov.pl/web/mswia/lista-ostrzezen",
                    source_name="TEST_SIMULATION",
                    detected_at=now - timedelta(days=1),
                    event_date=now - timedelta(days=2),
                    status="active",
                    sanctions_list="TEST_SIMULATION",
                )
            )
            added += 1
    db.commit()
    return added


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        added_c = 0
        for row in SAMPLES:
            exists = db.scalar(select(Company).where(Company.nip == row["nip"]))
            if exists:
                continue
            db.add(Company(**row))
            added_c += 1
        db.commit()
        print(f"Seed complete — added {added_c} companies.")

        ev_n = _seed_events_for_companies(db)
        if ev_n:
            print(f"Seed events — added {ev_n} risk_events.")
            for c in db.scalars(select(Company)).all():
                try:
                    recalculate_and_persist(db, c.id, lookback_days=90)
                except Exception as e:
                    print(f"recalculate {c.name}: {e}")
        else:
            print("Risk events unchanged (already present or no companies).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
