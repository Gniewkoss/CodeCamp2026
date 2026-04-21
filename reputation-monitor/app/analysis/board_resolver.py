"""Resolve current management / supervisory board from public knowledge.

The Polish KRS public REST API masks person names for GDPR
(``"K********"``, ``"J****"``) — but it **does publish** the first
letter, the exact length (via ``*`` padding) and, crucially, the full
role (``PREZES ZARZĄDU``, ``WICEPREZES DS. FINANSOWYCH`` …).

We use those three bits as a hard constraint for GPT-4o:

1. The model is given the whole masked roster.
2. For each seat it must return the real full name **consistent with the
   mask** — same first letter, same length. If it's not sure, leave that
   seat blank.
3. Back in Python we re-verify every name against the mask before we
   persist it. Anything that doesn't match the KRS signature is demoted
   to ``verified=False`` and the UI flags it accordingly.

This gives the user the thing they asked for: when we display a board
member, the fact that their first letter + length matches the official
KRS entry is proof they're a real person sitting on that seat (not a
hallucinated name).

Results are persisted as ``CompanyPerson`` rows with
``source="krs+llm"`` (verified) or ``source="llm_public"`` (unverified /
no KRS available). The governance scorer only trusts verified rows.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.llm import llm_available, llm_complete, llm_complete_with_web_search
from app.scraper.krs_client import PersonSignature

logger = logging.getLogger(__name__)


# ─── Data model ──────────────────────────────────────────────────────────


@dataclass
class ResolvedPerson:
    full_name: str
    role: str
    start_date: Optional[str] = None
    notes: Optional[str] = None
    organ: str = "zarząd"              # zarząd | rada_nadzorcza | prokurent
    verified: bool = False             # matches KRS mask?
    signature_idx: Optional[int] = None  # which KRS seat it's mapped to


@dataclass
class BoardResolution:
    persons: list[ResolvedPerson] = field(default_factory=list)
    confidence: str = "low"
    identity_note: str = ""
    used_ai: bool = False
    signatures_given: int = 0
    signatures_matched: int = 0


# ─── Prompt construction ────────────────────────────────────────────────


_BASE_SYSTEM = """Jesteś analitykiem due-diligence dla polskich firm. \
Twoim zadaniem jest podać PEŁNE imię i nazwisko każdej osoby wymienionej \
w składzie organów spółki pobranym z KRS — rejestr publikuje pierwszą \
literę i długość imienia/nazwiska (maska "F*****" = 6 znaków zaczynających \
się od F), a my potrzebujemy pełnej formy.

MASZ DOSTĘP DO NARZĘDZIA ``web_search``. UŻYWAJ GO AGRESYWNIE — zanim \
wypełnisz pola ``full_name`` musisz wykonać CO NAJMNIEJ 3–5 niezależnych \
wyszukiwań. Skład zarządu i rady nadzorczej zmienia się wielokrotnie \
w roku — wiedza wbudowana w model JEST NIEAKTUALNA.

Sugerowane zapytania (użyj wszystkich istotnych):
  • "{nazwa spółki} zarząd" (+ bieżący rok)
  • "{nazwa spółki} prezes zarządu"
  • "{nazwa spółki} wiceprezes"
  • "{nazwa spółki} rada nadzorcza"
  • "{nazwa spółki} site:{oficjalna domena} zarząd"
  • dla każdej maski której jeszcze nie rozwiązałeś — zapytanie \
    "{nazwa spółki} {pierwsza_litera_imienia}. {pierwsza_litera_nazwiska}."
  • dla międzynarodowych grup: "{nazwa spółki} board of directors"

Preferowane domeny: oficjalna strona spółki, ESPI/EBI (infostrefa.com, \
stockwatch.pl), bankier.pl, money.pl, rzeczpospolita.pl, pb.pl, \
businessinsider.com.pl, gpw.pl, rejestr.io, linkedin.com/in/. Sprawdź \
DATĘ publikacji — używaj najnowszych źródeł.

**Nie poprzestawaj na jednym wyszukiwaniu.** Jeśli po pierwszej turze \
masz nierozwiązane fotele — zadaj kolejne, bardziej precyzyjne pytania \
(np. "{spółka} skład zarządu 2026", "{spółka} nowy prezes 2025"). \
Celem jest wypełnienie JAK NAJWIĘKSZEJ liczby ``full_name`` zgodnych \
z maskami.

ZASADY KRYTYCZNE:
• Odpowiadaj WYŁĄCZNIE poprawnym JSON-em, bez tekstu przed/po. Żadnych \
  linków, żadnego markdown — tylko JSON.
• Każda proponowana osoba **polska** musi PASOWAĆ do maski: pierwsza \
  litera zgadza się, liczba znaków (włącznie z pierwszą literą) = \
  długość maski (łącznie z gwiazdkami). Jeśli NIE MASZ PEWNOŚCI co do \
  konkretnej osoby — zwróć pusty ``full_name`` dla tej pozycji.
• Dla osób o **obcym imieniu/nazwisku** (portugalskie, niemieckie, \
  francuskie, angielskie itd.) maska KRS często nie będzie pasować \
  długością, bo Polska transkrypcja różni się od oryginału (np. \
  "Pedro Soares de Pinho" vs maska "P**** S*****"). W takim przypadku \
  ustaw ``foreign: true``, podaj pełne **oryginalne** imię i nazwisko, \
  a my przyjmiemy je **tylko** jeśli ogólne ``confidence`` to "high". \
  Pierwsza litera imienia i pierwsza litera nazwiska MUSZĄ się zgadzać \
  z maską nawet dla obcokrajowców.
• NIE ZMYŚLAJ nazwisk tylko po to, żeby wypełnić odpowiedź. Lepiej pusto \
  niż fikcja. ``foreign: true`` nie zwalnia z obowiązku znajomości \
  konkretnej osoby — używaj go wyłącznie gdy naprawdę ją kojarzysz \
  z publicznych źródeł.
• Dla pozycji zarządu / RN użyj pełnej roli jaką podaje KRS \
  (``role_raw``) — nie zmieniaj jej.
• Dane czerpiesz z wiedzy publicznej: raporty bieżące ESPI, komunikaty \
  GPW, strony relacji inwestorskich, serwisy prasowe (Bankier, Money.pl, \
  Business Insider PL, Rzeczpospolita, Puls Biznesu, Reuters, FT, \
  publiczne profile LinkedIn kadry). Nie wymyślaj niczego spoza tej \
  wiedzy.

DANE WEJŚCIOWE:
  company: nazwa spółki
  krs: numer KRS
  nip: NIP (opcjonalnie)
  sector: sektor (opcjonalnie)
  seats: lista pozycji w organach z KRS. Każdy wpis zawiera:
    - seat_id: numer pozycji (używaj go w odpowiedzi),
    - organ: "zarząd" | "rada_nadzorcza" | "prokurent",
    - role_raw: dokładna nazwa roli z KRS,
    - first_name_mask: maska imienia ("I*******"),
    - last_name_mask: maska nazwiska ("F*****"),
    - second_name_mask: opcjonalna maska drugiego imienia,
    - notes: dodatkowy kontekst (np. treść prokury, gdzie mogą być \
      niezamaskowane imiona innych prokurentów).

WYMAGANY SCHEMA:
{
  "persons": [
    {
      "seat_id": <int>,
      "full_name": "Imię Nazwisko",     // pusty string jeśli brak pewności
      "start_year": 2023,               // opcjonalnie
      "foreign": false,                 // true jeśli obce imię/nazwisko
      "notes": "krótka nota (1 zdanie)"
    }
  ],
  "confidence": "low" | "medium" | "high",
  "identity_note": "krótkie uzasadnienie pewności + data wiedzy"
}

NIE wymieniaj osób, których nie ma w ``seats``. Zwróć wpis dla KAŻDEGO \
seat_id, nawet jeśli z pustym ``full_name``.
"""


def _build_user_payload(
    company_name: str,
    *,
    krs: Optional[str],
    nip: Optional[str],
    sector: Optional[str],
    signatures: list[PersonSignature],
) -> str:
    seats = []
    for i, s in enumerate(signatures, start=1):
        seats.append(
            {
                "seat_id": i,
                "organ": s.organ,
                "role_raw": s.role,
                "first_name_mask": s.first_name_mask,
                "last_name_mask": s.last_name_mask,
                "second_name_mask": s.second_name_mask or None,
                "notes": s.notes[:800] if s.notes else None,
            }
        )
    payload = {
        "company": company_name,
        "krs": krs,
        "nip": nip,
        "sector": sector,
        "seats": seats,
    }
    return json.dumps(payload, ensure_ascii=False)


# ─── Unconstrained fallback prompt (when there are no signatures) ────────


_UNCONSTRAINED_SYSTEM = """Jesteś analitykiem due-diligence polskich firm. \
Wypisz AKTUALNY, PUBLICZNIE ZNANY skład zarządu i rady nadzorczej \
podanej spółki. Masz dostęp do narzędzia ``web_search`` — UŻYJ GO \
(oficjalna strona, ESPI, GPW, Bankier, Money, Puls Biznesu, Rzeczpospolita). \
Jeśli po przeszukaniu dalej nie wiesz — zostaw listę pustą, nigdy nie \
zmyślaj.

SCHEMA:
{
  "persons": [
    {"full_name": "Imię Nazwisko", "role": "Prezes Zarządu",
     "start_year": 2023, "notes": "krótka nota"}
  ],
  "confidence": "low"|"medium"|"high",
  "identity_note": "uzasadnienie + data wiedzy"
}

Odpowiadaj jednym JSON-em, bez tekstu przed/po."""


# ─── Helpers ─────────────────────────────────────────────────────────────


def _extract_json(raw: str) -> Optional[dict[str, Any]]:
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def _split_full_name(full: str) -> tuple[str, str]:
    """Naive first/last name split that's good enough for masked
    validation. Multi-word first names ("Jan Paweł") keep the first token
    as first name; double-barrelled surnames stay intact as last name."""
    tokens = [t for t in re.split(r"\s+", (full or "").strip()) if t]
    if len(tokens) < 2:
        return "", ""
    first = tokens[0]
    last = " ".join(tokens[1:])
    return first, last


# ─── Public entry point ─────────────────────────────────────────────────


def resolve_board(
    *,
    company_name: str,
    sector: Optional[str] = None,
    krs: Optional[str] = None,
    nip: Optional[str] = None,
    signatures: Optional[list[PersonSignature]] = None,
) -> BoardResolution:
    """Resolve board members using KRS masks as a constraint.

    * When ``signatures`` are supplied (company has a KRS number and we
      fetched its odpis), we run the constrained prompt that forces
      GPT-4o to return a name for each seat consistent with the mask.
    * Without signatures we fall back to the old free-form prompt (used
      for companies added via quick-lookup without a KRS).

    Returns a :class:`BoardResolution` whose ``persons`` only contains
    entries with non-empty full names. Each person carries
    ``verified=True`` iff the name passes :func:`PersonSignature.matches`.
    """
    if not company_name:
        return BoardResolution(identity_note="brak nazwy spółki")
    if not llm_available():
        return BoardResolution(identity_note="brak klucza LLM (OpenAI / Anthropic)")

    signatures = signatures or []
    if signatures:
        return _resolve_with_signatures(company_name, krs, nip, sector, signatures)
    return _resolve_unconstrained(company_name, krs, nip, sector)


def _resolve_with_signatures(
    company_name: str,
    krs: Optional[str],
    nip: Optional[str],
    sector: Optional[str],
    signatures: list[PersonSignature],
) -> BoardResolution:
    user = _build_user_payload(
        company_name, krs=krs, nip=nip, sector=sector, signatures=signatures
    )
    # Web search eats tokens (model reasoning + tool input/output), so we
    # give the resolver a much bigger budget than the old offline path.
    raw = llm_complete_with_web_search(
        system=_BASE_SYSTEM,
        user=user,
        max_tokens=6000,
        purpose="board_resolver_masked",
    )
    if not raw:
        return BoardResolution(
            confidence="low",
            identity_note="LLM zwrócił pustą odpowiedź",
            used_ai=True,
            signatures_given=len(signatures),
        )

    data = _extract_json(raw) or {}
    confidence = str(data.get("confidence") or "low").lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    identity_note = str(data.get("identity_note") or "")[:400]

    persons_out: list[ResolvedPerson] = []
    matched = 0
    for p in (data.get("persons") or [])[:40]:
        if not isinstance(p, dict):
            continue
        seat_id = p.get("seat_id")
        if not isinstance(seat_id, int) or seat_id < 1 or seat_id > len(signatures):
            continue
        full = str(p.get("full_name") or "").strip()
        if not full or "*" in full:
            continue
        sig = signatures[seat_id - 1]
        first, last = _split_full_name(full)
        verified = sig.matches(first, last)
        is_foreign = bool(p.get("foreign"))
        if verified:
            matched += 1
        # If the model broke the mask badly, drop the suggestion — we'd
        # rather show the masked KRS row than a wrong name. Exception: for
        # foreign-named board members (common in subsidiaries of
        # international groups) the mask length won't match because Polish
        # KRS stores an ASCII-folded form. We let these through when:
        #   • the model flagged ``foreign: true`` per person,
        #   • it has overall ``confidence: high``, AND
        #   • at minimum the first initials of first- and last-name align
        #     with the mask (the cheapest sanity check we can still do).
        if not verified and sig.is_masked:
            first_ok = (
                bool(first) and bool(sig.first_initial)
                and first[0].lower() == sig.first_initial.lower()
            )
            last_segment = re.split(r"[\s\-]+", last or "")[0]
            last_ok = (
                bool(last_segment) and bool(sig.last_initial)
                and last_segment[0].lower() == sig.last_initial.lower()
            )
            if is_foreign and confidence == "high" and first_ok and last_ok:
                logger.info(
                    "Board resolver: accepting foreign seat %d (%s) as %r "
                    "(mask mismatch tolerated due to transliteration)",
                    seat_id, sig.display, full,
                )
                verified = False  # still mark as unverified in UI
            else:
                logger.info(
                    "Board resolver: mask mismatch for seat %d (%s) — model said %r",
                    seat_id, sig.display, full,
                )
                continue
        # Normalise start_year.
        sy = p.get("start_year")
        start_date: Optional[str] = None
        if isinstance(sy, (int, float)) and 1980 <= int(sy) <= 2100:
            start_date = str(int(sy))
        elif isinstance(sy, str) and re.fullmatch(r"\d{4}", sy.strip()):
            start_date = sy.strip()
        persons_out.append(
            ResolvedPerson(
                full_name=full[:200],
                role=sig.role[:128] or "—",
                organ=sig.organ,
                start_date=start_date,
                notes=str(p.get("notes") or "")[:400] or None,
                verified=verified,
                signature_idx=seat_id,
            )
        )

    # When nothing matched AND confidence is low → drop everything. But if
    # some seats were matched, keep only those (silently dropping fakes
    # produced for the remaining seats).
    if not persons_out:
        return BoardResolution(
            confidence=confidence,
            identity_note=(
                identity_note
                or "LLM nie rozpoznał składu, nawet z maskami KRS."
            ),
            used_ai=True,
            signatures_given=len(signatures),
            signatures_matched=0,
        )

    return BoardResolution(
        persons=persons_out,
        confidence=confidence if matched > 0 else "low",
        identity_note=identity_note,
        used_ai=True,
        signatures_given=len(signatures),
        signatures_matched=matched,
    )


def _resolve_unconstrained(
    company_name: str,
    krs: Optional[str],
    nip: Optional[str],
    sector: Optional[str],
) -> BoardResolution:
    raw = llm_complete_with_web_search(
        system=_UNCONSTRAINED_SYSTEM,
        user=json.dumps(
            {"company": company_name, "krs": krs, "nip": nip, "sector": sector},
            ensure_ascii=False,
        ),
        max_tokens=1400,
        purpose="board_resolver_free",
    )
    if not raw:
        return BoardResolution(identity_note="LLM zwrócił pustą odpowiedź", used_ai=True)
    data = _extract_json(raw) or {}
    confidence = str(data.get("confidence") or "low").lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    identity_note = str(data.get("identity_note") or "")[:400]
    persons_raw = data.get("persons") or []
    if confidence == "low" or not isinstance(persons_raw, list):
        return BoardResolution(
            confidence=confidence,
            identity_note=identity_note or "brak pewnej wiedzy publicznej",
            used_ai=True,
        )

    persons: list[ResolvedPerson] = []
    for p in persons_raw[:20]:
        if not isinstance(p, dict):
            continue
        full = str(p.get("full_name") or "").strip()
        if not full or "*" in full or len(full) < 4 or len(full.split()) < 2:
            continue
        role = str(p.get("role") or "—").strip()[:128] or "—"
        organ = "rada_nadzorcza" if "nadzor" in role.lower() else "zarząd"
        sy = p.get("start_year")
        start_date = None
        if isinstance(sy, (int, float)) and 1980 <= int(sy) <= 2100:
            start_date = str(int(sy))
        elif isinstance(sy, str) and re.fullmatch(r"\d{4}", sy.strip()):
            start_date = sy.strip()
        persons.append(
            ResolvedPerson(
                full_name=full[:200],
                role=role,
                organ=organ,
                start_date=start_date,
                notes=str(p.get("notes") or "")[:400] or None,
                verified=False,  # no KRS mask to verify against
                signature_idx=None,
            )
        )

    return BoardResolution(
        persons=persons,
        confidence=confidence,
        identity_note=identity_note,
        used_ai=True,
    )
