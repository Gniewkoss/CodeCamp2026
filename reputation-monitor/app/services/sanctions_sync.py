"""Create / update risk_events from sanctions screening.

Two passes are performed:

1. **Fuzzy name match** against EU / OFAC / UN / MSWiA combined index
   (handled by ``check_names``).
2. **Jurisdiction-link heuristic** — any commercial exposure to a
   sanctioned jurisdiction (Russia, Belarus, DPRK, Iran, Syria, Crimea,
   Donetsk, Luhansk, occupied territories) surfaced through article
   analysis, contract counterparties or company address is itself a
   red flag of highest severity, even if the company name is not on any
   list. The user explicitly asked: "jak jest jakiekolwiek połączenie
   z rosją to już firma ma bardzo wysoki risk".
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.event_types import EVENT_TYPES
from app.analysis.sanctions_checker import SanctionsMatch, check_names
from app.models import (
    Article,
    ArticleAnalysis,
    Company,
    CompanyPerson,
    Contract,
    RiskEvent,
)

logger = logging.getLogger(__name__)


# Jurisdiction patterns — Polish + English + native forms. We require the
# match to be a whole word so innocent substrings (e.g. "Russia" inside
# "Prussia") don't false-positive.
_JURISDICTIONS: list[tuple[str, list[str]]] = [
    (
        "Rosja",
        [
            r"rosj\w*",
            r"rosyjsk\w*",
            r"\bRussia\b",
            r"\bRussian\b",
            r"\bMoskw\w*",
            r"\bMoscow\b",
            r"\bSankt\-?Petersburg\w*",
            r"\bKreml\w*",
            r"Федерац\w*",
            r"Россия",
        ],
    ),
    (
        "Białoruś",
        [
            r"białorus\w*",
            r"bialorus\w*",
            r"\bBelarus\b",
            r"\bBelarusian\b",
            r"\bMińsk\b",
            r"\bMinsk\b",
            r"Беларусь",
        ],
    ),
    (
        "Iran",
        [r"\bIran\b", r"\bIrański\w*", r"\bIranian\b", r"\bTeheran\w*", r"\bTehran\b"],
    ),
    (
        "Korea Północna",
        [
            r"korea\s+p(ó|o)łnocn\w*",
            r"p(ó|o)łnocn\w*\s+kore\w*",
            r"\bDPRK\b",
            r"North\s+Korea",
            r"\bPhenian\w*",
            r"\bPyongyang\b",
        ],
    ),
    ("Syria", [r"\bSyri\w*", r"Syrian"]),
    (
        "Krym / okupowane terytoria Ukrainy",
        [
            r"\bKrym\w*",
            r"\bCrimea\b",
            r"\bDoniec\w*",
            r"\bDonets?k\b",
            r"\bLuhan\w*",
            r"\bŁugańsk\w*",
            r"\bZaporo[żz]\w*",
            r"\bChersoń\w*",
            r"\bKherson\b",
        ],
    ),
]

_RUSSIAN_ENTITY_NAMES: list[str] = [
    # Sanctioned Russian banks & industrials frequently co-mentioned.
    "Sberbank",
    "VTB",
    "Gazprom",
    "Rosneft",
    "Lukoil",
    "Alfa-Bank",
    "Alfabank",
    "Promsvyazbank",
    "Novatek",
    "Rosatom",
    "Wagner",
    "Rostec",
    "Rosoboronexport",
]

_LINK_HINT_WORDS_RE = re.compile(
    r"(udział\w*|udzial\w*|współprac\w*|wspolprac\w*|dostaw\w*|odbior\w*|"
    r"kontrahent\w*|klient\w*|partner\w*|spółk\w*\s+zale\w*|sp(ó|o)łka\s+matk\w*|"
    r"właścicie\w*|wlascicie\w*|kontrol\w*|powiąza\w*|powiaza\w*|"
    r"sprzed\w*|eksport\w*|import\w*|umow\w*|kontrak\w*|inwestyc\w*|"
    r"subsidiar\w*|ownership|owner|stake|partner|supplier|customer|"
    r"parent|linked\s+to|connected\s+to|trade|deal|contract)",
    re.IGNORECASE | re.UNICODE,
)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/_=-]{24,}\b")  # base64 / UUIDs / tracking ids


def _clean_for_matching(text: str) -> str:
    """Strip URLs, HTML tags, and long base64-like tokens — these trigger
    spurious word-match hits (e.g. ``Rosj`` appearing inside a Google News
    tracking URL)."""
    if not text:
        return ""
    t = _URL_RE.sub(" ", text)
    t = _HTML_TAG_RE.sub(" ", t)
    t = _LONG_TOKEN_RE.sub(" ", t)
    return t


def _find_jurisdiction_hits(text: str) -> list[tuple[str, str, int, int]]:
    """Return (jurisdiction, snippet, match_start, match_end) for each
    sanctioned-country mention. Positions refer to the **cleaned** text so
    callers can open a local window around the hit for proximity checks.
    """
    if not text:
        return []
    clean = _clean_for_matching(text)
    if not clean.strip():
        return []
    hits: list[tuple[str, str, int, int]] = []
    for label, patterns in _JURISDICTIONS:
        for pat in patterns:
            m = re.search(pat, clean, flags=re.IGNORECASE | re.UNICODE)
            if m:
                start = max(0, m.start() - 80)
                end = min(len(clean), m.end() + 80)
                snippet = clean[start:end].replace("\n", " ").strip()
                hits.append((label, snippet[:240], m.start(), m.end()))
                break  # only one snippet per label per document
    return hits


# Patterns that almost always indicate generic geopolitical / commodity
# commentary rather than a company-specific link. If the snippet matches
# ONE of these AND the company name isn't nearby, we drop the signal.
_GEOPOLITICAL_FILLER_RE = re.compile(
    r"(atak\s+na|wojn\w+\s+(w|z|na)|cen\w*\s+(ropy|gazu|paliw)|"
    r"sankcj\w*\s+(UE|USA|Zachod\w*)|Komisj\w*\s+Europejsk\w*|"
    r"decyzj\w*\s+(USA|UE|Zachod\w*)|NATO|Ukrain\w+|"
    r"konflikt\s+(zbrojn\w*|w|na)|inwazj\w*|agresj\w*\s+Rosj\w*|"
    r"oil\s+price|gas\s+price|war\s+in|attack\s+on|EU\s+sanctions|US\s+sanctions)",
    re.IGNORECASE | re.UNICODE,
)


def _alias_patterns(names: Iterable[str]) -> list[re.Pattern[str]]:
    """Build anchored regexes for company name + aliases.

    Drops the legal-form suffixes (S.A., Sp. z o.o., etc.) so that an article
    saying "PKN Orlen podpisał umowę" matches the configured "PKN Orlen SA".
    Names shorter than 3 chars are skipped to avoid pathological matches.
    """
    pats: list[re.Pattern[str]] = []
    suffix_re = re.compile(
        r"\s+(S\.?A\.?|Sp(\.|ółk[aą])?\s*z\s*o\.?o\.?|SA|SKA|SJ)\s*$",
        re.IGNORECASE,
    )
    seen: set[str] = set()
    for raw in names:
        if not raw:
            continue
        base = suffix_re.sub("", raw).strip()
        if len(base) < 3:
            continue
        key = base.lower()
        if key in seen:
            continue
        seen.add(key)
        # Escape, but keep as an inexact word-boundary match.
        pats.append(re.compile(rf"(?<!\w){re.escape(base)}(?!\w)", re.IGNORECASE))
    return pats


def _company_near(
    text: str, start: int, end: int, alias_pats: list[re.Pattern[str]], window: int = 400
) -> bool:
    if not alias_pats:
        return False
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    chunk = text[lo:hi]
    return any(p.search(chunk) for p in alias_pats)


def _find_russian_entities(text: str) -> list[str]:
    if not text:
        return []
    clean = _clean_for_matching(text)
    if not clean.strip():
        return []
    found: list[str] = []
    for ent in _RUSSIAN_ENTITY_NAMES:
        if re.search(rf"(?<!\w){re.escape(ent)}(?!\w)", clean, flags=re.IGNORECASE):
            found.append(ent)
    return found


def _scan_jurisdiction_links(
    db: Session, company: Company
) -> list[tuple[str, str, str, str]]:
    """Return (jurisdiction, evidence, source_url, source_name).

    Decision logic per article:

    * Find Russia/Iran/DPRK/… mentions with their character positions.
    * Open a ±400 char window around each hit.
    * Fire a RiskEvent only if **within that window** we also see
        (a) a sanctioned-entity name (Sberbank, Gazprom, Rosatom…), OR
        (b) BOTH a commercial-linkage verb ("kontrakt", "dostawa", "udział"…)
            AND the company's own name / alias.
    * If the snippet is dominated by "atak na Iran", "wojna w Rosji",
      "sankcje UE", "Komisja Europejska", etc. (pure geopolitical filler)
      and the company name isn't in the local window, drop it.
    """
    out: list[tuple[str, str, str, str]] = []
    seen_jur: set[str] = set()

    alias_pats = _alias_patterns([company.name, *(company.aliases or [])])

    # 1) Article analyses — only ones the LLM confirmed mention the company.
    rows = db.execute(
        select(Article, ArticleAnalysis)
        .join(ArticleAnalysis, ArticleAnalysis.article_id == Article.id)
        .where(Article.company_id == company.id)
        .where(ArticleAnalysis.mentions_company.is_(True))
        .order_by(Article.scraped_at.desc())
        .limit(120)
    ).all()
    for art, an in rows:
        blob_parts = [
            art.title or "",
            art.content or "",
            an.summary or "",
        ]
        if an.key_facts:
            try:
                blob_parts.extend(str(x) for x in (an.key_facts or []))
            except Exception:
                pass
        blob = " \n ".join(p for p in blob_parts if p)
        clean = _clean_for_matching(blob)
        hits = _find_jurisdiction_hits(blob)
        rus_ent_global = _find_russian_entities(blob)
        if not hits and not rus_ent_global:
            continue

        for jur, snippet, m_start, m_end in hits:
            if jur in seen_jur:
                continue

            lo = max(0, m_start - 400)
            hi = min(len(clean), m_end + 400)
            window = clean[lo:hi]

            has_company_nearby = _company_near(clean, m_start, m_end, alias_pats)
            has_link_verb_nearby = bool(_LINK_HINT_WORDS_RE.search(window))
            has_sanctioned_entity_nearby = any(
                re.search(rf"(?<!\w){re.escape(ent)}(?!\w)", window, re.IGNORECASE)
                for ent in _RUSSIAN_ENTITY_NAMES
            )
            is_geopolitical_filler = bool(_GEOPOLITICAL_FILLER_RE.search(window))

            # Strongest signal: sanctioned entity co-occurs in the same
            # window → always fire (even if company name is missing, this
            # is about sanctioned counterparty risk surfaced in coverage).
            accept = has_sanctioned_entity_nearby

            # Company-specific commercial link: both the company name AND
            # a trade/ownership verb must sit near the jurisdiction mention.
            if not accept and has_company_nearby and has_link_verb_nearby:
                accept = True

            # Pure geopolitical commentary without company context → skip.
            if is_geopolitical_filler and not has_company_nearby and not has_sanctioned_entity_nearby:
                accept = False

            if not accept:
                logger.debug(
                    "Skipping jurisdiction hit '%s' in article %s — "
                    "company_nearby=%s link_verb_nearby=%s sanctioned_entity=%s filler=%s",
                    jur,
                    art.id,
                    has_company_nearby,
                    has_link_verb_nearby,
                    has_sanctioned_entity_nearby,
                    is_geopolitical_filler,
                )
                continue

            seen_jur.add(jur)
            out.append((jur, snippet, art.url or "", art.source or "prasa"))

        # Separate pass for sanctioned-entity co-mentions (independent of
        # jurisdiction regex — e.g. article mentions "Gazprom" without
        # saying "Rosja").
        for ent in rus_ent_global:
            key = f"Rosja:{ent}"
            if key in seen_jur:
                continue
            # Still require the company name near the entity mention — a
            # generic industry story listing competitors shouldn't taint
            # the company's risk profile.
            ent_m = re.search(rf"(?<!\w){re.escape(ent)}(?!\w)", clean, re.IGNORECASE)
            if not ent_m:
                continue
            if not _company_near(clean, ent_m.start(), ent_m.end(), alias_pats, window=500):
                continue
            seen_jur.add(key)
            ctx_lo = max(0, ent_m.start() - 120)
            ctx_hi = min(len(clean), ent_m.end() + 120)
            out.append(
                (
                    "Rosja",
                    f"Wzmianka o podmiocie objętym sankcjami: {ent}. "
                    f"{clean[ctx_lo:ctx_hi].replace(chr(10), ' ').strip()[:240]}",
                    art.url or "",
                    art.source or "prasa",
                )
            )

    # 2) Contract counterparties — no proximity check needed; the counter-
    # party IS the company's direct contractual link.
    contracts = db.scalars(
        select(Contract)
        .where(Contract.company_id == company.id)
        .order_by(Contract.detected_at.desc())
        .limit(200)
    ).all()
    for c in contracts:
        text = " ".join(filter(None, [c.counterparty or "", c.title or "", (c.url or "")]))
        hits = _find_jurisdiction_hits(text)
        rus_ent = _find_russian_entities(text)
        for jur, snippet, _s, _e in hits:
            key = f"contract:{jur}:{c.id}"
            if key in seen_jur:
                continue
            seen_jur.add(key)
            out.append(
                (
                    jur,
                    f"Kontrakt: {c.counterparty or c.title or c.id} — {snippet}",
                    c.url or "",
                    f"{c.source or 'CONTRACT'}",
                )
            )
        for ent in rus_ent:
            key = f"contract-ent:{ent}:{c.id}"
            if key in seen_jur:
                continue
            seen_jur.add(key)
            out.append(
                (
                    "Rosja",
                    f"Kontrahent pod sankcjami: {ent}. {c.title or ''}",
                    c.url or "",
                    f"{c.source or 'CONTRACT'}",
                )
            )

    # 3) Company address / aliases — a direct registry signal.
    ctext = " ".join(filter(None, [company.address or "", *(company.aliases or [])]))
    for jur, snippet, _s, _e in _find_jurisdiction_hits(ctext):
        key = f"addr:{jur}"
        if key in seen_jur:
            continue
        seen_jur.add(key)
        out.append((jur, f"Dane rejestrowe: {snippet}", "", "registry"))

    return out


def apply_sanctions_check(db: Session, company_id: str) -> List[RiskEvent]:
    company = db.get(Company, company_id)
    if not company:
        return []
    persons = list(
        db.scalars(
            select(CompanyPerson).where(
                CompanyPerson.company_id == company_id,
                CompanyPerson.is_active.is_(True),
            )
        ).all()
    )
    cnames = [company.name] + list(company.aliases or [])
    pnames = [p.full_name for p in persons]
    try:
        matches = check_names(company_names=cnames, person_names=pnames)
    except Exception as e:
        logger.warning("Sanctions check failed: %s", e)
        matches = []

    created: list[RiskEvent] = []
    now = datetime.now(timezone.utc)

    # Pass 1: direct list matches.
    for m in matches:
        et = "sanctions_match_company" if m.match_type == "company" else "sanctions_match_person"
        title = f"Sankcje: trafienie ({m.match_type}) — {m.matched_entity[:80]}"
        dup = db.scalar(
            select(RiskEvent).where(
                RiskEvent.company_id == company_id,
                RiskEvent.event_type == et,
                RiskEvent.title == title,
                RiskEvent.status == "active",
            )
        )
        if dup:
            continue
        ev = RiskEvent(
            company_id=company_id,
            event_type=et,
            title=title[:512],
            description=f"Dopasowanie fuzzy {m.match_score:.0f}% do wpisu: {m.matched_entity}",
            severity=float(EVENT_TYPES.get(et, 0.85)),
            source_url="https://www.gov.pl/web/mswia/lista-ostrzezen",
            source_name=m.list_name,
            detected_at=now,
            status="active",
            sanctions_list=m.list_name[:64],
            related_person=None if m.match_type == "company" else m.matched_entity[:512],
        )
        db.add(ev)
        created.append(ev)

    # Pass 2: jurisdiction-link heuristic. Any connection to a sanctioned
    # jurisdiction is itself a top-severity red flag.
    try:
        jur_hits = _scan_jurisdiction_links(db, company)
    except Exception as e:
        logger.warning("Jurisdiction link scan failed: %s", e)
        jur_hits = []

    # Retract previously-raised "active" jurisdiction links whose jur label
    # is no longer in the fresh result. The old heuristic fired on any
    # Russia/Iran mention; the new one requires local proximity to the
    # company name or a sanctioned entity. This cleanup ensures users
    # stop seeing stale false positives after a rescan.
    fresh_titles = {
        f"Powiązanie z jurysdykcją objętą sankcjami: {jur}" for jur, _e, _u, _s in jur_hits
    }
    stale_events = db.scalars(
        select(RiskEvent).where(
            RiskEvent.company_id == company_id,
            RiskEvent.event_type == "sanctioned_jurisdiction_link",
            RiskEvent.status == "active",
        )
    ).all()
    for se in stale_events:
        if se.title not in fresh_titles:
            se.status = "retracted"
            se.resolution_note = (
                "Wycofane automatycznie: zaostrzona heurystyka nie potwierdziła "
                "bezpośredniego powiązania spółki z daną jurysdykcją w aktualnych artykułach."
            )

    for jur, evidence, url, src in jur_hits:
        title = f"Powiązanie z jurysdykcją objętą sankcjami: {jur}"
        dup = db.scalar(
            select(RiskEvent).where(
                RiskEvent.company_id == company_id,
                RiskEvent.event_type == "sanctioned_jurisdiction_link",
                RiskEvent.title == title,
                RiskEvent.status == "active",
            )
        )
        if dup:
            continue
        ev = RiskEvent(
            company_id=company_id,
            event_type="sanctioned_jurisdiction_link",
            title=title[:512],
            description=(evidence or "")[:2000],
            severity=float(EVENT_TYPES.get("sanctioned_jurisdiction_link", 0.98)),
            source_url=(url or "")[:512] or None,
            source_name=src[:128] if src else None,
            detected_at=now,
            status="active",
            sanctions_list=f"heurystyka: {jur}"[:64],
        )
        db.add(ev)
        created.append(ev)

    # Commit even if no *new* events were created — we may have retracted
    # stale false positives that still need to be persisted.
    try:
        db.commit()
        for e in created:
            db.refresh(e)
    except Exception as exc:
        logger.warning("Sanctions commit failed: %s", exc)
        db.rollback()
    return created
