"""Polish National Court Register (KRS) — public REST API.

Besides the raw JSON fetch + heuristic person walk, this module exposes
:func:`extract_person_signatures`. That function returns per-seat records
from *Dział 2* with **first-letter + length of the GDPR-masked name**
plus the **full public role** and the parent organ (zarząd / rada
nadzorcza / prokurenci). Those signatures are a strong constraint we
feed to the GPT-4o board resolver so it can only return real names that
match what KRS already publishes — no more "trust me, this is Orlen's
CEO" guesses.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

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


_MASK_CHAR_RE = re.compile(r"\*")

# Some data sources (press releases, older imports, certain KRS sections
# when the full name is under legal protection) publish names reduced to
# initials with dots, e.g. ``"J. K."`` or ``"A.B."``. We treat those as
# valid masked identities — the person exists, we just don't know the full
# name. The regex matches `J. K.` / `J.K.` / `J K.` etc. (case-insensitive).
_INITIALS_RE = re.compile(r"^([A-ZĄĆĘŁŃÓŚŹŻ])\.\s*([A-ZĄĆĘŁŃÓŚŹŻ])\.?$", re.IGNORECASE)


def is_initials_only(name: str | None) -> bool:
    """Return ``True`` if ``name`` is of the ``"J. K."`` / ``"A.B."`` shape.

    Used both by the signature extractor and by the UI layer to decide
    whether to keep a masked person entity instead of throwing it away.
    """
    if not name:
        return False
    return bool(_INITIALS_RE.match(name.strip()))


def split_initials(name: str) -> tuple[str, str] | None:
    """Return ``(first_initial, last_initial)`` for an ``"J. K."`` string or
    ``None`` if the shape doesn't match."""
    if not name:
        return None
    m = _INITIALS_RE.match(name.strip())
    if not m:
        return None
    return m.group(1).upper(), m.group(2).upper()


@dataclass
class PersonSignature:
    """Per-seat record extracted from KRS Dział 2.

    All positional fields are what KRS publishes for any member of an organ:
    the first letter of first/last name, the number of characters behind the
    mask (``*``), and the **full** role. We never persist PESEL and explicitly
    forbid the resolver from returning it.
    """

    organ: str                         # "zarząd" | "rada_nadzorcza" | "prokurent"
    role: str                          # e.g. "PREZES ZARZĄDU"
    first_name_mask: str = ""          # e.g. "I*******"
    last_name_mask: str = ""           # e.g. "F*****"
    second_name_mask: Optional[str] = None  # some people have two given names
    notes: str = ""                    # extra payload (e.g. "PROKURA ODDZIAŁOWA …")
    position_idx: int = 0              # index within the organ (for UI ordering)

    @property
    def first_initial(self) -> str:
        return self.first_name_mask[:1].upper()

    @property
    def last_initial(self) -> str:
        return self.last_name_mask[:1].upper()

    @property
    def first_name_len(self) -> int:
        return len(self.first_name_mask)

    @property
    def last_name_len(self) -> int:
        return len(self.last_name_mask)

    @property
    def is_masked(self) -> bool:
        return "*" in self.first_name_mask or "*" in self.last_name_mask

    @property
    def display(self) -> str:
        im = self.first_name_mask
        nz = self.last_name_mask
        return f"{im} {nz}".strip() or "—"

    def matches(self, first_name: str, last_name: str) -> bool:
        """Return ``True`` iff ``first_name`` / ``last_name`` are consistent
        with our KRS mask (same first letter and same length).

        We ignore diacritics when comparing the first letter (``Ł`` vs ``L``
        etc.) because KRS stores everything in uppercase ASCII-ish form.
        """
        if not first_name or not last_name:
            return False
        fn = first_name.strip()
        ln = last_name.strip()
        if self.first_name_mask:
            if len(fn) != self.first_name_len:
                return False
            if _strip_diacritics(fn[0]).upper() != _strip_diacritics(self.first_initial).upper():
                return False
        if self.last_name_mask:
            # Double-barrelled surnames ("Kowalski-Nowak") — KRS publishes
            # only the first segment for the mask length but the public name
            # may include both. We're lenient: accept if the first segment
            # matches.
            first_segment = re.split(r"[\s\-]+", ln)[0]
            if len(first_segment) != self.last_name_len:
                return False
            if _strip_diacritics(first_segment[0]).upper() != _strip_diacritics(self.last_initial).upper():
                return False
        return True


_DIACRITIC_MAP = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "aceLnoszZACELNOSZZ".replace("L", "l").replace("L", "L"),  # noop, see below
)
# The translate table above is tricky because L/Ł; do it explicitly:
_DIACRITIC_MAP = {
    ord("ą"): "a", ord("Ą"): "A",
    ord("ć"): "c", ord("Ć"): "C",
    ord("ę"): "e", ord("Ę"): "E",
    ord("ł"): "l", ord("Ł"): "L",
    ord("ń"): "n", ord("Ń"): "N",
    ord("ó"): "o", ord("Ó"): "O",
    ord("ś"): "s", ord("Ś"): "S",
    ord("ź"): "z", ord("Ź"): "Z",
    ord("ż"): "z", ord("Ż"): "Z",
}


def _strip_diacritics(s: str) -> str:
    return (s or "").translate(_DIACRITIC_MAP)


def _mask_from_value(raw: Any) -> str:
    """Take a KRS name field (usually ``{"imie": "I*******"}`` or a plain
    string) and return the underlying masked string verbatim.

    We preserve the ``*`` characters — that's exactly how we learn the
    real name's length. We *also* normalise the dotted-initials form
    (``"J."``, ``"J. K."``) into a single-letter mask so downstream code
    can treat it uniformly with the KRS ``I*******`` style.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        s = raw.strip()
        # "J. K." → "J" — we don't know the length, keep just the initial
        # so ``first_initial`` still works. ``is_masked`` will return True.
        if _INITIALS_RE.match(s):
            logger.debug("krs: normalising dotted initials %r", s)
            # Caller will split into first/last separately — for the most
            # common case of a single "J." value in an `imie` field we
            # return the bare initial.
            return s.split(".", 1)[0].strip().upper()
        if re.fullmatch(r"[A-ZĄĆĘŁŃÓŚŹŻ]\.?", s, flags=re.IGNORECASE):
            return s.rstrip(".").upper()
        return s
    if isinstance(raw, dict):
        for key in (
            "nazwiskoICzlon", "nazwiskoIICzlon", "nazwiskoPierwszyCzlon",
            "nazwisko", "nazwa", "imie", "imieDrugie", "imiona",
            "wartosc", "value",
        ):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return _mask_from_value(v)
        return ""
    if isinstance(raw, list):
        parts = [_mask_from_value(x) for x in raw]
        return " ".join(p for p in parts if p)
    return str(raw).strip()


def signature_from_initials(
    initials: str,
    *,
    organ: str = "zarząd",
    role: str = "CZŁONEK ZARZĄDU",
    position_idx: int = 0,
) -> PersonSignature | None:
    """Build a :class:`PersonSignature` from a bare ``"J. K."`` string.

    Returns ``None`` for anything that doesn't match the initials pattern.
    The resulting signature is intentionally *masked* (first-letter only,
    unknown length) so the UI keeps it as a stub entity instead of
    dropping the person altogether.
    """
    pair = split_initials(initials)
    if not pair:
        return None
    first_initial, last_initial = pair
    logger.info(
        "krs: preserving masked person from initials %r (organ=%s, role=%s)",
        initials, organ, role,
    )
    return PersonSignature(
        organ=organ,
        role=role,
        first_name_mask=first_initial,
        last_name_mask=last_initial,
        notes="",
        position_idx=position_idx,
    )


def extract_person_signatures(data: dict[str, Any]) -> list[PersonSignature]:
    """Walk ``dzial2`` of a KRS :func:`fetch_krs_odpis_json` response and
    return one :class:`PersonSignature` per seat.

    Preserves the original order as stored in KRS — the resolver uses that
    order to correlate positions with external public knowledge.
    """
    out: list[PersonSignature] = []
    try:
        dzial2 = data["odpis"]["dane"]["dzial2"]
    except (KeyError, TypeError):
        return out

    # ── 1. Zarząd (organ uprawniony do reprezentacji) ────────────────────
    reprez = dzial2.get("reprezentacja") or {}
    if isinstance(reprez, dict):
        organ_label = (reprez.get("nazwaOrganu") or "ZARZĄD").strip().lower()
        # Most private companies are plain "zarząd"; foundations sometimes
        # have a single-person "KIEROWNIK" organ, still goes under zarząd.
        kind = "zarząd"
        for idx, member in enumerate(reprez.get("sklad") or [], start=1):
            if not isinstance(member, dict):
                continue
            imiona = member.get("imiona") or {}
            first = _mask_from_value(imiona.get("imie") if isinstance(imiona, dict) else imiona)
            second = _mask_from_value(imiona.get("imieDrugie") if isinstance(imiona, dict) else None)
            last = _mask_from_value(member.get("nazwisko"))
            role = str(member.get("funkcjaWOrganie") or organ_label or "CZŁONEK ZARZĄDU").strip()
            out.append(
                PersonSignature(
                    organ=kind,
                    role=role,
                    first_name_mask=first,
                    last_name_mask=last,
                    second_name_mask=second or None,
                    position_idx=idx,
                )
            )

    # ── 2. Rada nadzorcza ────────────────────────────────────────────────
    nadzor = dzial2.get("organNadzoru")
    nadzor_list: list[dict[str, Any]]
    if isinstance(nadzor, list):
        nadzor_list = [n for n in nadzor if isinstance(n, dict)]
    elif isinstance(nadzor, dict):
        nadzor_list = [nadzor]
    else:
        nadzor_list = []
    for organ in nadzor_list:
        for idx, member in enumerate(organ.get("sklad") or [], start=1):
            if not isinstance(member, dict):
                continue
            imiona = member.get("imiona") or {}
            first = _mask_from_value(imiona.get("imie") if isinstance(imiona, dict) else imiona)
            second = _mask_from_value(imiona.get("imieDrugie") if isinstance(imiona, dict) else None)
            last = _mask_from_value(member.get("nazwisko"))
            # KRS doesn't store a per-person role for RN; organ name is all
            # we can show ("Członek Rady Nadzorczej").
            out.append(
                PersonSignature(
                    organ="rada_nadzorcza",
                    role="CZŁONEK RADY NADZORCZEJ",
                    first_name_mask=first,
                    last_name_mask=last,
                    second_name_mask=second or None,
                    position_idx=idx,
                )
            )

    # ── 3. Prokurenci ───────────────────────────────────────────────────
    # Interesting: ``rodzajProkury`` free-text often lists OTHER prokurenci
    # with their full public names + PESEL. We surface it in ``notes`` but
    # do not try to cross-resolve here (the resolver will use it).
    prokurenci = dzial2.get("prokurenci")
    if isinstance(prokurenci, list):
        for idx, member in enumerate(prokurenci, start=1):
            if not isinstance(member, dict):
                continue
            imiona = member.get("imiona") or {}
            first = _mask_from_value(imiona.get("imie") if isinstance(imiona, dict) else imiona)
            last = _mask_from_value(member.get("nazwisko"))
            out.append(
                PersonSignature(
                    organ="prokurent",
                    role=str(member.get("rodzajProkury") or "PROKURENT").strip()[:240],
                    first_name_mask=first,
                    last_name_mask=last,
                    notes=str(member.get("rodzajProkury") or "")[:2000],
                    position_idx=idx,
                )
            )

    return out


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
