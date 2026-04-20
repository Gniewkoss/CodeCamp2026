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


def _as_text(v: Any) -> str:
    """Flatten KRS name-ish fields into a clean string. KRS JSON sometimes
    returns plain strings, sometimes ``{'wartosc': 'Jan'}`` wrappers, and
    sometimes lists — the old parser happily wrote those dicts into the DB
    (producing ``{'imie': 'J****'}`` in the UI). We unwrap them here and
    reject anything we can't render."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for key in ("wartosc", "value", "text", "imie", "nazwisko", "nazwiskoCzlon"):
            if key in v and isinstance(v[key], (str, int, float)):
                return str(v[key]).strip()
        return ""
    if isinstance(v, list):
        return " ".join(_as_text(x) for x in v if x).strip()
    return ""


def extract_persons_from_krs_blob(data: Any) -> list[dict[str, Any]]:
    """Heuristic walk for imiona/nazwisko + funkcja fields in KRS JSON."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # Accept every realistic variant Polish KRS JSON uses — including the
    # suffixed "nazwiskoCzlon" / "imionaCzlon" that appear in member-of-board
    # blocks ("Członek Zarządu"). Match by prefix so we don't drop future
    # schema additions like "nazwiskoPierwszeCzlon" etc.
    nazwisko_prefixes = ("nazwisko", "nazwiska", "nazwisk")
    imie_prefixes = ("imion", "imie", "imię", "imiepierwsze", "imiepierwszy")
    funkcja_prefixes = ("funkcja", "funkcjawo", "funkcjawor")

    def _find_prefix(kl: dict[str, str], prefixes: tuple[str, ...]) -> Any:
        for k_lower, k_real in kl.items():
            stripped = k_lower.replace("_", "")
            for pref in prefixes:
                if stripped.startswith(pref):
                    return kl.get(k_real) if False else k_real  # return real key
        return None

    def visit(x: Any) -> None:
        if isinstance(x, dict):
            kl = {k.lower(): k for k in x}
            nk = _find_prefix(kl, nazwisko_prefixes)
            ik = _find_prefix(kl, imie_prefixes)
            naz = _as_text(x.get(nk)) if nk else ""
            im = _as_text(x.get(ik)) if ik else ""
            if naz:
                name = (f"{im} {naz}" if im else naz).strip()
                if name:
                    fk = _find_prefix(kl, funkcja_prefixes)
                    role = _as_text(x.get(fk)) if fk else ""
                    key = (name.lower(), role[:64].lower())
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
