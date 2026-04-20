"""Polish National Court Register (KRS) — public REST API."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

KRS_BASE = "https://api-krs.ms.gov.pl/api/krs/OdpisAktualny"


def normalise_krs(krs: str | None) -> str | None:
    if not krs:
        return None
    digits = re.sub(r"\D", "", krs)
    if not digits:
        return None
    return digits.zfill(10)


def fetch_krs_odpis_json(krs: str) -> dict[str, Any]:
    num = normalise_krs(krs)
    if not num:
        raise ValueError("Invalid KRS")
    url = f"{KRS_BASE}/{num}"
    headers = {"User-Agent": "ReputationMonitor/1.0 (due-diligence demo)"}
    # KRS API responds with UTF-8 bytes but without a proper `charset` header →
    # force UTF-8 decoding to avoid Polish-diacritic mojibake ("SPÓŁKA" → "SPA?A?KA").
    # Try both registers: P=Rejestr Przedsiębiorców, S=Stowarzyszenia.
    with httpx.Client(timeout=45.0, follow_redirects=True) as client:
        for rejestr in ("P", "S"):
            r = client.get(url, params={"rejestr": rejestr, "format": "json"}, headers=headers)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            body = r.content.decode("utf-8", errors="replace").strip()
            if not body:
                continue
            data = json.loads(body)
            if data:
                return data
    raise LookupError(f"KRS {num} not found in P or S register")


def extract_persons_from_krs_blob(data: Any) -> list[dict[str, Any]]:
    """Heuristic walk for imiona/nazwisko + funkcja fields in KRS JSON."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def visit(x: Any) -> None:
        if isinstance(x, dict):
            kl = {k.lower(): k for k in x}
            naz = None
            im = None
            for nk in ("nazwisko", "nazwisk"):
                if nk in kl:
                    naz = x.get(kl[nk])
                    break
            for ik in ("imiona", "imie", "imię"):
                if ik in kl:
                    im = x.get(kl[ik])
                    break
            if naz and str(naz).strip():
                name = f"{im or ''} {naz}".strip()
                role = None
                for rk in ("funkcjaworganie", "funkcja", "funkcjaWOrganie"):
                    for k in x:
                        if k.lower().replace("_", "") == rk.lower().replace("_", ""):
                            role = x.get(k)
                            break
                    if role:
                        break
                key = (name.lower(), (role or "")[:64].lower())
                if key not in seen:
                    seen.add(key)
                    found.append({"full_name": name, "role": role or "—"})
            for v in x.values():
                visit(v)
        elif isinstance(x, list):
            for i in x:
                visit(i)

    visit(data)
    return found


def extract_krs_highlights(data: dict[str, Any]) -> dict[str, Any]:
    """Best-effort fields for UI (strings safe for display)."""
    flat = str(data)[:20000]
    out: dict[str, Any] = {"raw_excerpt": flat[:1200]}
    # Try common nesting
    odpis = data.get("odpis") or data.get("Odpis") or data
    dane = odpis.get("dane") if isinstance(odpis, dict) else None
    if isinstance(dane, dict):
        d1 = dane.get("dzial1") or {}
        if isinstance(d1, dict):
            dane_podm = d1.get("danePodmiotu") or d1.get("danePodmiot") or {}
            if isinstance(dane_podm, dict):
                out["legal_form"] = dane_podm.get("formaPrawna") or dane_podm.get("formaPrawnaNazwa")
                out["share_capital"] = dane_podm.get("wysokoscKapitaluZakladowego") or dane_podm.get(
                    "wysokoscKapitaluZakladowegoWZlotych"
                )
                out["registration_date"] = dane_podm.get("dataRejestracjiWKRS")
    return out
