"""Seed sample Polish companies."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Company


SAMPLES = [
    {
        "name": "PKN Orlen SA",
        "aliases": ["PKN Orlen", "Orlen", "Orlen SA", "PKN"],
        "nip": "7740001454",
        "krs": "0000028860",
    },
    {
        "name": "KGHM Polska Miedź SA",
        "aliases": ["KGHM", "KGHM Polska Miedź", "Polska Miedź"],
        "nip": "6920000213",
        "krs": "0000014243",
    },
    {
        "name": "mBank SA",
        "aliases": ["mBank", "Millennium Bank", "Bank Millennium"],
        "nip": "5260215088",
        "krs": "0000010272",
    },
    {
        "name": "CD Projekt SA",
        "aliases": ["CD Projekt", "CDPR", "CD Projekt Red"],
        "nip": "7342867148",
        "krs": "0000006862",
    },
    {
        "name": "LPP SA",
        "aliases": ["LPP", "Reserved", "Cropp", "Sinsay", "House"],
        "nip": "5860208581",
        "krs": "0000028577",
    },
]


def main() -> None:
    db = SessionLocal()
    try:
        for row in SAMPLES:
            exists = db.scalar(select(Company).where(Company.nip == row["nip"]))
            if exists:
                continue
            c = Company(
                name=row["name"],
                aliases=row["aliases"],
                nip=row["nip"],
                krs=row["krs"],
            )
            db.add(c)
        db.commit()
        print("Seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
