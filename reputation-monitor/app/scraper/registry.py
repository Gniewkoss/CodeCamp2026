"""Polish business-registry lookup — three sources, merged:

1. **MF white-list** (Wykaz podatników VAT) — `wl-api.mf.gov.pl`
   Public, no auth. NIP, REGON, KRS, VAT status, address, bank accounts.

2. **GUS BIR REGON** (`wyszukiwarkaregon.stat.gov.pl`) — SOAP.
   Uses GUS's published public test key by default; override via
   GUS_BIR_API_KEY + GUS_BIR_API_URL for production data. Gives PKD,
   legal form, registration date, parent unit, etc.

3. **CEIDG v3** (`dane.biznes.gov.pl/api/ceidg/v3`) — REST + JWT.
   The only canonical source for sole traders (JDG) — opt-in via
   CEIDG_API_TOKEN.

Public API:
    lookup_query(q)    -> RegistryRecord | None  (smart dispatcher, merged)
    lookup_nip(nip)    -> RegistryRecord | None
    lookup_regon(reg)  -> RegistryRecord | None
    guess_aliases_from_name(name) -> list[str]
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Optional

import httpx
from lxml import etree

from app.config import get_settings
from app.scraper.krs_client import fetch_krs_odpis_json

logger = logging.getLogger(__name__)

WL_BASE = "https://wl-api.mf.gov.pl"


# ──────────────────────────────────────────────────────────────────
# Data class
# ──────────────────────────────────────────────────────────────────

@dataclass
class RegistryRecord:
    name: str
    nip: Optional[str] = None
    regon: Optional[str] = None
    krs: Optional[str] = None
    legal_form: Optional[str] = None
    status_vat: Optional[str] = None
    address: Optional[str] = None
    registration_date: Optional[str] = None
    pkd_primary: Optional[str] = None
    pkd_primary_label: Optional[str] = None
    pkd_all: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d

    def merge(self, other: "RegistryRecord") -> "RegistryRecord":
        """In-place fill-in of empty fields from `other`. Never overwrites."""
        for f in ("name", "nip", "regon", "krs", "legal_form", "status_vat",
                  "address", "registration_date", "pkd_primary",
                  "pkd_primary_label"):
            if not getattr(self, f) and getattr(other, f):
                setattr(self, f, getattr(other, f))
        if not self.pkd_all and other.pkd_all:
            self.pkd_all = other.pkd_all
        for s in other.sources:
            if s not in self.sources:
                self.sources.append(s)
        for k, v in (other.raw or {}).items():
            self.raw.setdefault(k, v)
        return self


# ──────────────────────────────────────────────────────────────────
# Input normalisation
# ──────────────────────────────────────────────────────────────────

def normalise_nip(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) == 10 else None


def normalise_regon(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) in (9, 14) else None


def normalise_krs(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits.zfill(10) if 1 <= len(digits) <= 10 else None


# ──────────────────────────────────────────────────────────────────
# 1) MF white-list (VAT)
# ──────────────────────────────────────────────────────────────────

def _mf_subject_to_record(subject: dict[str, Any]) -> RegistryRecord:
    working_addr = subject.get("workingAddress") or subject.get("residenceAddress") or ""
    name = (subject.get("name") or "").strip()
    return RegistryRecord(
        name=name,
        nip=subject.get("nip"),
        regon=subject.get("regon"),
        krs=subject.get("krs"),
        status_vat=subject.get("statusVat"),
        address=str(working_addr).strip() or None,
        registration_date=subject.get("registrationLegalDate") or subject.get("registrationDenialDate"),
        sources=["MF_WHITE_LIST"],
        raw={"mf_white_list": subject},
    )


def _mf_lookup(kind: str, value: str) -> Optional[RegistryRecord]:
    today = date.today().isoformat()
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(f"{WL_BASE}/api/search/{kind}/{value}", params={"date": today})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.info("MF lookup failed (%s %s): %s", kind, value, e)
        return None
    subject = (data.get("result") or {}).get("subject") or {}
    if not subject or not subject.get("name"):
        return None
    return _mf_subject_to_record(subject)


def mf_lookup_nip(nip: str) -> Optional[RegistryRecord]:
    n = normalise_nip(nip)
    return _mf_lookup("nip", n) if n else None


def mf_lookup_regon(regon: str) -> Optional[RegistryRecord]:
    r_ = normalise_regon(regon)
    return _mf_lookup("regon", r_) if r_ else None


# ──────────────────────────────────────────────────────────────────
# 2) GUS BIR REGON (SOAP)
# ──────────────────────────────────────────────────────────────────
# Reference: https://api.stat.gov.pl/Home/RegonApi

_SOAP_NS = "http://CIS/BIR/PUBL/2014/07"
_ENV_NS = "http://www.w3.org/2003/05/soap-envelope"   # SOAP 1.2
_WSA_NS = "http://www.w3.org/2005/08/addressing"
_ACTION_BASE = f"{_SOAP_NS}/IUslugaBIRzewnPubl"


def _gus_envelope(url: str, action: str, action_body: str, sid: Optional[str] = None) -> str:
    """Build a SOAP 1.2 envelope with WS-Addressing headers (required by GUS)."""
    sid_node = f'<sid xmlns="{_SOAP_NS}">{sid}</sid>' if sid else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{_ENV_NS}" xmlns:wsa="{_WSA_NS}" xmlns:ns="{_SOAP_NS}">'
        '<soap:Header>'
        f'<wsa:To>{url}</wsa:To>'
        f'<wsa:Action>{_ACTION_BASE}/{action}</wsa:Action>'
        f'{sid_node}'
        '</soap:Header>'
        f'<soap:Body>{action_body}</soap:Body>'
        '</soap:Envelope>'
    )


def _gus_call(url: str, action: str, body: str, sid: Optional[str] = None) -> Optional[str]:
    envelope = _gus_envelope(url, action, body, sid)
    headers = {
        "Content-Type": f'application/soap+xml;charset=UTF-8;action="{_ACTION_BASE}/{action}"',
        "Accept": "application/soap+xml",
    }
    if sid:
        headers["sid"] = sid
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(url, content=envelope.encode("utf-8"), headers=headers)
            r.raise_for_status()
            return r.text
    except Exception as e:
        logger.info("GUS BIR %s failed: %s", action, e)
        return None


def _gus_login(url: str, key: str) -> Optional[str]:
    body = f"<ns:Zaloguj><ns:pKluczUzytkownika>{key}</ns:pKluczUzytkownika></ns:Zaloguj>"
    resp = _gus_call(url, "Zaloguj", body)
    if not resp:
        return None
    m = re.search(r"<ZalogujResult[^>]*>([^<]+)</ZalogujResult>", resp)
    sid = m.group(1).strip() if m else None
    if not sid:
        logger.info("GUS BIR login returned no sid; response head=%r", resp[:200])
    return sid


def _gus_search(url: str, sid: str, *, nip: Optional[str] = None, regon: Optional[str] = None, krs: Optional[str] = None) -> Optional[dict[str, str]]:
    params: list[str] = []
    if nip:
        params.append(f"<dat:Nip>{nip}</dat:Nip>")
    if regon:
        tag = "Regon" if len(regon) == 9 else "Regon14"
        params.append(f"<dat:{tag}>{regon}</dat:{tag}>")
    if krs:
        params.append(f"<dat:Krs>{krs}</dat:Krs>")
    if not params:
        return None
    body = (
        '<ns:DaneSzukajPodmioty>'
        '<ns:pParametryWyszukiwania xmlns:dat="http://CIS/BIR/PUBL/2014/07/DataContract">'
        f'{"".join(params)}'
        '</ns:pParametryWyszukiwania>'
        '</ns:DaneSzukajPodmioty>'
    )
    resp = _gus_call(url, "DaneSzukajPodmioty", body, sid=sid)
    if not resp:
        return None
    m = re.search(r"<DaneSzukajPodmiotyResult[^>]*>(.*?)</DaneSzukajPodmiotyResult>", resp, re.DOTALL)
    if not m:
        return None
    inner = m.group(1)
    # Unescape XML entities
    inner = (inner.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    # The inner XML looks like: <root><dane>...</dane></root>
    try:
        root = etree.fromstring(inner.encode("utf-8"))
    except Exception as e:
        logger.info("GUS BIR DaneSzukaj parse failed: %s; inner=%r", e, inner[:200])
        return None
    dane = root.find("dane")
    if dane is None:
        return None
    out: dict[str, str] = {}
    for el in dane:
        if el.text and el.text.strip():
            out[el.tag] = el.text.strip()
    return out


def _gus_full_report(url: str, sid: str, regon: str, report_name: str) -> dict[str, str]:
    body = (
        '<ns:DanePobierzPelnyRaport>'
        f'<ns:pRegon>{regon}</ns:pRegon>'
        f'<ns:pNazwaRaportu>{report_name}</ns:pNazwaRaportu>'
        '</ns:DanePobierzPelnyRaport>'
    )
    resp = _gus_call(url, "DanePobierzPelnyRaport", body, sid=sid)
    if not resp:
        return {}
    m = re.search(r"<DanePobierzPelnyRaportResult[^>]*>(.*?)</DanePobierzPelnyRaportResult>", resp, re.DOTALL)
    if not m:
        return {}
    inner = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    try:
        root = etree.fromstring(inner.encode("utf-8"))
    except Exception:
        return {}
    dane = root.find("dane")
    if dane is None:
        return {}
    # For PKD report there can be multiple <dane> nodes; iterate all siblings too
    out: dict[str, str] = {}
    for entry in root.iter("dane"):
        for el in entry:
            if el.text and el.text.strip():
                out.setdefault(el.tag, el.text.strip())
    return out


def _gus_pkd_list(url: str, sid: str, regon: str, report: str) -> list[dict[str, Any]]:
    body = (
        '<ns:DanePobierzPelnyRaport>'
        f'<ns:pRegon>{regon}</ns:pRegon>'
        f'<ns:pNazwaRaportu>{report}</ns:pNazwaRaportu>'
        '</ns:DanePobierzPelnyRaport>'
    )
    resp = _gus_call(url, "DanePobierzPelnyRaport", body, sid=sid)
    if not resp:
        return []
    m = re.search(r"<DanePobierzPelnyRaportResult[^>]*>(.*?)</DanePobierzPelnyRaportResult>", resp, re.DOTALL)
    if not m:
        return []
    inner = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    try:
        root = etree.fromstring(inner.encode("utf-8"))
    except Exception:
        return []
    pkds: list[dict[str, Any]] = []
    for entry in root.iter("dane"):
        code = None
        label = None
        primary = False
        for el in entry:
            tag = el.tag
            val = (el.text or "").strip()
            if tag.endswith("PkdKod") or tag.endswith("praw_pkdKod") or tag.endswith("fiz_pkdKod"):
                code = val
            elif tag.endswith("PkdNazwa") or tag.endswith("praw_pkdNazwa") or tag.endswith("fiz_pkdNazwa"):
                label = val
            elif tag.endswith("PkdPrzewazajace") or tag.endswith("praw_pkdPrzewazajace") or tag.endswith("fiz_pkdPrzewazajace"):
                primary = val in ("1", "true", "True")
        if code:
            pkds.append({"code": code, "label": label, "primary": primary})
    return pkds


def _gus_logout(url: str, sid: str) -> None:
    body = f"<ns:Wyloguj><ns:pIdentyfikatorSesji>{sid}</ns:pIdentyfikatorSesji></ns:Wyloguj>"
    _gus_call(url, "Wyloguj", body, sid=sid)


_LEGAL_FORM_LABELS = {
    "SP.Z O.O.": "Spółka z ograniczoną odpowiedzialnością",
    "SP Z O O": "Spółka z ograniczoną odpowiedzialnością",
    "SPÓŁKA AKCYJNA": "Spółka akcyjna",
    "SPOLKA AKCYJNA": "Spółka akcyjna",
    "SA": "Spółka akcyjna",
    "S.A.": "Spółka akcyjna",
    "JEDNOOSOBOWA DZIAŁALNOŚĆ GOSPODARCZA": "Jednoosobowa działalność gospodarcza",
}


def gus_lookup(*, nip: Optional[str] = None, regon: Optional[str] = None, krs: Optional[str] = None) -> Optional[RegistryRecord]:
    """Look up a Polish business in GUS BIR REGON (SOAP).

    Returns a RegistryRecord with `pkd_*` and legal form populated. Falls back
    silently on any network / parse error.
    """
    settings = get_settings()
    if not settings.gus_bir_enabled:
        return None
    url = settings.gus_bir_api_url
    key = settings.gus_bir_api_key
    if not key:
        return None

    sid = _gus_login(url, key)
    if not sid:
        return None
    try:
        summary = _gus_search(url, sid, nip=nip, regon=regon, krs=krs)
        if not summary:
            return None

        name = summary.get("Nazwa") or ""
        out_regon = summary.get("Regon") or regon or ""
        out_nip = summary.get("Nip") or nip or ""
        silos = summary.get("SilosID")   # 1=JDG, 2=OS. Prawna, 6=Pozostałe
        typ = summary.get("Typ")  # F|P|LP (fizyczna, prawna, lokalna)
        legal_form_raw = summary.get("NazwaPodstawowejFormyPrawnej") or summary.get("NazwaSzczegolnejFormyPrawnej")
        address_parts = [
            summary.get("Ulica") or "",
            summary.get("NrNieruchomosci") or "",
            summary.get("KodPocztowy") or "",
            summary.get("Miejscowosc") or "",
        ]
        address = ", ".join(p for p in address_parts if p) or None
        reg_date = summary.get("DataZakonczeniaDzialalnosci") or None  # only when closed

        # Pick correct full report for PKD list
        if typ == "P":
            report = "BIR11OsPrawnaPkd"
        elif typ == "F":
            report = "BIR11OsFizycznaPkd"
        else:
            report = "BIR11OsPrawnaPkd"
        pkds = _gus_pkd_list(url, sid, out_regon, report) if out_regon else []

        # Primary PKD
        primary = next((p for p in pkds if p.get("primary")), pkds[0] if pkds else None)

        legal_form = _LEGAL_FORM_LABELS.get((legal_form_raw or "").upper().strip(), legal_form_raw or None)
        if not legal_form and silos == "1":
            legal_form = "Jednoosobowa działalność gospodarcza"

        return RegistryRecord(
            name=name,
            nip=out_nip or None,
            regon=out_regon or None,
            krs=summary.get("KRS") or None,
            legal_form=legal_form,
            address=address,
            registration_date=reg_date,
            pkd_primary=primary["code"] if primary else None,
            pkd_primary_label=primary.get("label") if primary else None,
            pkd_all=pkds or [],
            sources=["GUS_BIR"],
            raw={"gus_bir": summary},
        )
    finally:
        try:
            _gus_logout(url, sid)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────
# 3) CEIDG v3 — sole traders
# ──────────────────────────────────────────────────────────────────

def ceidg_lookup(*, nip: Optional[str] = None, regon: Optional[str] = None) -> Optional[RegistryRecord]:
    settings = get_settings()
    token = settings.ceidg_api_token
    if not token:
        return None  # silently unavailable unless user provides a token
    params: dict[str, str] = {"limit": "1"}
    if nip:
        params["nip"] = nip
    if regon:
        params["regon"] = regon
    if not params:
        return None
    url = f"{settings.ceidg_api_url.rstrip('/')}/firmy"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, params=params, headers=headers)
            if r.status_code in (404, 204):
                return None
            r.raise_for_status()
            # CEIDG returns 200 + JSON for hits; 204 / empty body = no JDG match
            if not (r.content or b"").strip():
                return None
            data = r.json()
    except Exception as e:
        logger.info("CEIDG lookup failed: %s", e)
        return None
    if isinstance(data, list):
        firmy = data
    else:
        firmy = data.get("firmy") or data.get("wynik") or []
    if not firmy:
        return None
    f = firmy[0]
    name = (f.get("nazwa") or "").strip()
    owner = f.get("wlasciciel") or {}
    owner_name = " ".join(x for x in [owner.get("imie"), owner.get("nazwisko")] if x)
    display_name = name or owner_name or "(JDG)"
    addr = f.get("adresGlownegoMiejscaWykonywaniaDzialalnosci") or f.get("adresDoKorespondencji") or {}
    address_str = ", ".join(
        str(x) for x in [addr.get("ulica"), addr.get("budynek"), addr.get("kod"), addr.get("miasto")] if x
    )
    pkds_raw = f.get("pkd") or []
    pkds: list[dict[str, Any]] = []
    for p in pkds_raw:
        if isinstance(p, dict):
            pkds.append({"code": p.get("kod") or p.get("code"), "label": p.get("nazwa"), "primary": bool(p.get("glowny"))})
        elif isinstance(p, str):
            pkds.append({"code": p, "label": None, "primary": False})
    primary = next((p for p in pkds if p.get("primary")), pkds[0] if pkds else None)

    return RegistryRecord(
        name=display_name,
        nip=f.get("nip") or nip,
        regon=f.get("regon") or regon,
        krs=None,
        legal_form="Jednoosobowa działalność gospodarcza",
        status_vat=f.get("statusVat"),
        address=address_str or None,
        registration_date=f.get("dataRozpoczeciaWykonywaniaDzialalnosci"),
        pkd_primary=primary["code"] if primary else None,
        pkd_primary_label=primary.get("label") if primary else None,
        pkd_all=pkds,
        sources=["CEIDG"],
        raw={"ceidg": f},
    )


# ──────────────────────────────────────────────────────────────────
# 4) KRS public API (ms.gov.pl) — no auth, works out of the box
# ──────────────────────────────────────────────────────────────────

def _walk_find_str(data: Any, *names: str) -> Optional[str]:
    """BFS: first scalar value whose key matches any `names` (case-insensitive)."""
    wanted = {n.lower() for n in names}
    stack: list[Any] = [data]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(k, str) and k.lower() in wanted and isinstance(v, (str, int)) and str(v).strip():
                    return str(v).strip()
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)
    return None


def _walk_find_dict(data: Any, *names: str) -> Optional[dict]:
    wanted = {n.lower() for n in names}
    stack: list[Any] = [data]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(k, str) and k.lower() in wanted and isinstance(v, dict):
                    return v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(x, list):
            stack.extend(x)
    return None


def _krs_address(blob: Any) -> Optional[str]:
    adr = _walk_find_dict(blob, "adres", "siedziba") or {}
    # "siedziba" typically has nested "adres"
    if not adr.get("ulica") and isinstance(adr.get("adres"), dict):
        adr = adr["adres"]
    parts = [
        adr.get("ulica"),
        adr.get("nrDomu") or adr.get("numerBudynku"),
        adr.get("kodPocztowy"),
        adr.get("miejscowosc") or adr.get("miasto"),
    ]
    s = ", ".join(str(p) for p in parts if p)
    return s or None


def _krs_pkd_list(blob: Any) -> list[dict[str, Any]]:
    pkds: list[dict[str, Any]] = []
    przed = _walk_find_dict(blob, "przedmiotDzialalnosci") or {}
    # Primary
    prim = przed.get("przedmiotPrzewazajacejDzialalnosci") or []
    for p in prim if isinstance(prim, list) else [prim]:
        if isinstance(p, dict):
            code = p.get("kodPKD") or p.get("kod")
            label = p.get("opis") or p.get("nazwaPKD") or p.get("nazwa")
            if code:
                pkds.append({"code": str(code), "label": label, "primary": True})
    # Secondary
    other = przed.get("przedmiotPozostalejDzialalnosci") or []
    for p in other if isinstance(other, list) else [other]:
        if isinstance(p, dict):
            code = p.get("kodPKD") or p.get("kod")
            label = p.get("opis") or p.get("nazwaPKD") or p.get("nazwa")
            if code:
                pkds.append({"code": str(code), "label": label, "primary": False})
    return pkds


def krs_direct_lookup(krs: str) -> Optional[RegistryRecord]:
    """Public KRS REST API — works without any API key."""
    k = normalise_krs(krs)
    if not k:
        return None
    try:
        blob = fetch_krs_odpis_json(k)
    except Exception as e:
        logger.info("KRS direct lookup failed for %s: %s", k, e)
        return None
    if not blob:
        return None

    name = _walk_find_str(blob, "nazwa") or ""
    nip = _walk_find_str(blob, "nip")
    regon = _walk_find_str(blob, "regon")
    legal_form = _walk_find_str(blob, "formaPrawna", "formaPrawnaNazwa")
    reg_date = _walk_find_str(blob, "dataRejestracjiWKRS")
    pkds = _krs_pkd_list(blob)
    primary = next((p for p in pkds if p.get("primary")), pkds[0] if pkds else None)

    return RegistryRecord(
        name=name,
        nip=nip,
        regon=regon,
        krs=k,
        legal_form=legal_form,
        address=_krs_address(blob),
        registration_date=reg_date,
        pkd_primary=primary["code"] if primary else None,
        pkd_primary_label=primary.get("label") if primary else None,
        pkd_all=pkds,
        sources=["KRS"],
        raw={"krs_excerpt": str(blob)[:2000]},
    )


# ──────────────────────────────────────────────────────────────────
# Combined lookup — chain of sources with merge
# ──────────────────────────────────────────────────────────────────

def lookup_nip(nip: str) -> Optional[RegistryRecord]:
    n = normalise_nip(nip)
    if not n:
        return None
    rec = mf_lookup_nip(n)
    # CEIDG for sole traders who may or may not show up on MF white-list
    ceidg = ceidg_lookup(nip=n)
    if ceidg:
        rec = rec.merge(ceidg) if rec else ceidg
    # GUS BIR always for richer data (PKD, legal form)
    gus = gus_lookup(nip=n)
    if gus:
        rec = rec.merge(gus) if rec else gus
    return rec


def lookup_regon(regon: str) -> Optional[RegistryRecord]:
    r_ = normalise_regon(regon)
    if not r_:
        return None
    rec = mf_lookup_regon(r_)
    ceidg = ceidg_lookup(regon=r_)
    if ceidg:
        rec = rec.merge(ceidg) if rec else ceidg
    gus = gus_lookup(regon=r_)
    if gus:
        rec = rec.merge(gus) if rec else gus
    return rec


def lookup_krs(krs: str) -> Optional[RegistryRecord]:
    """Lookup by KRS using the public KRS REST API, then enrich via MF/GUS."""
    k = normalise_krs(krs)
    if not k:
        return None
    rec = krs_direct_lookup(k)
    # Enrich via MF white-list through the KRS-derived NIP (gives VAT status, address, etc.)
    if rec and rec.nip:
        mf = mf_lookup_nip(rec.nip)
        if mf:
            rec.merge(mf)
    # GUS still used when the key is configured (better PKD / legal form)
    gus = gus_lookup(krs=k)
    if gus:
        rec = rec.merge(gus) if rec else gus
    return rec


_KRS_QUERY_PREFIX = re.compile(r"^\s*(?:krs|krs\s*:|krs\s*nr\s*:?|krs\s*=)\s*", re.IGNORECASE)


def lookup_query(query: str) -> Optional[RegistryRecord]:
    """Smart dispatcher: recognises NIP, REGON, KRS (by prefix or leading zeros).

    Priority:
      1. Explicit prefix "KRS" / "KRS:" / "KRS=" / "KRS NR" → KRS API
      2. 10-digit string starting with "0000..." → KRS API
      3. 10 digits → NIP (with KRS fallback if MF returns nothing)
      4. 9 or 14 digits → REGON
      5. otherwise `None` (name-based search handled elsewhere)
    """
    q = (query or "").strip()
    if not q:
        return None

    # 1) explicit prefix
    m = _KRS_QUERY_PREFIX.match(q)
    if m:
        rest = q[m.end():]
        digits = re.sub(r"\D", "", rest)
        return lookup_krs(digits) if digits else None

    digits = re.sub(r"\D", "", q)

    # 2) leading-zero pattern — very strong KRS indicator
    if len(digits) == 10 and digits.startswith("00"):
        return lookup_krs(digits)

    # 3) 10 digits → NIP, but fall back to KRS if MF returns nothing
    if len(digits) == 10:
        rec = lookup_nip(digits)
        if rec:
            return rec
        return lookup_krs(digits)

    # 4) REGON
    if len(digits) in (9, 14):
        return lookup_regon(digits)

    # 5) short KRS (without leading zeros, user typed e.g. "327813")
    if digits and 1 <= len(digits) <= 10:
        return lookup_krs(digits)

    return None


# ──────────────────────────────────────────────────────────────────
# Aliases heuristic
# ──────────────────────────────────────────────────────────────────

def guess_aliases_from_name(name: str) -> list[str]:
    if not name:
        return []
    cleaned = re.sub(
        r"\b(spółka akcyjna|spolka akcyjna|sa|s\.a\.|sp\. z o\.o\.|spółka z ograniczoną odpowiedzialnością|"
        r"sp z oo|z o\.o\.|spółka z o\. o\.)\b",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    aliases: list[str] = []
    if cleaned and cleaned.lower() != name.lower():
        aliases.append(cleaned)
    words = [w for w in re.split(r"\s+", cleaned) if len(w) >= 2]
    if len(words) >= 2:
        acronym = "".join(w[0] for w in words[:4] if w[0].isalpha()).upper()
        if 2 <= len(acronym) <= 6 and acronym not in aliases:
            aliases.append(acronym)
    big_words = [w for w in words if len(w) >= 4 and w.lower() not in {"grupa", "group", "polska", "polski"}]
    if big_words and big_words[0] not in aliases:
        aliases.append(big_words[0])
    return aliases[:6]
