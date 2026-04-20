"""Sanctions list download, cache (24h), and fuzzy name matching.

Uses rapidfuzz.fuzz.token_sort_ratio with cutoff 85 (spec).
Caches XML/HTML under settings.sanctions_cache_dir (e.g. /data/sanctions in Docker).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import httpx
from bs4 import BeautifulSoup
from lxml import etree
from rapidfuzz import fuzz, process

from app.config import get_settings

logger = logging.getLogger(__name__)

EU_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
OFAC_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
MSWIA_URL = "https://www.gov.pl/web/mswia/lista-ostrzezen"

CACHE_FILES = {
    "eu": "eu_fsfp.xml",
    "ofac": "sdn.xml",
    "un": "un_consolidated.xml",
    "mswia": "mswia_list.html",
}
META_FILE = "meta.json"
REFRESH_SEC = 24 * 3600


@dataclass
class SanctionsMatch:
    matched: bool
    match_score: float
    matched_entity: str
    list_name: str
    match_type: str  # "company" | "person"


def _cache_dir() -> Path:
    p = Path(get_settings().sanctions_cache_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _meta_path() -> Path:
    return _cache_dir() / META_FILE


def _load_meta() -> dict[str, Any]:
    mp = _meta_path()
    if not mp.exists():
        return {}
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(meta: dict[str, Any]) -> None:
    _meta_path().write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _needs_refresh(key: str) -> bool:
    meta = _load_meta()
    ts = meta.get(key)
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() > REFRESH_SEC


def _download(url: str, dest: Path) -> None:
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        with client.stream("GET", url, headers={"User-Agent": "ReputationMonitor/1.0"}) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)


def ensure_sanctions_cache() -> None:
    """Download / refresh sanctions files if older than 24h."""
    base = _cache_dir()
    meta = _load_meta()
    now = datetime.now(timezone.utc).isoformat()

    pairs = [
        ("eu", EU_URL, base / CACHE_FILES["eu"]),
        ("ofac", OFAC_URL, base / CACHE_FILES["ofac"]),
        ("un", UN_URL, base / CACHE_FILES["un"]),
        ("mswia", MSWIA_URL, base / CACHE_FILES["mswia"]),
    ]
    for key, url, path in pairs:
        if not _needs_refresh(key) and path.exists():
            continue
        try:
            logger.info("Downloading sanctions feed %s → %s", key, path.name)
            _download(url, path)
            meta[key] = now
        except Exception as e:
            logger.warning("Sanctions download failed (%s): %s", key, e)
    _save_meta(meta)


def _parse_eu_names(path: Path) -> list[str]:
    names: list[str] = []
    if not path.exists():
        return names
    try:
        for _, el in etree.iterparse(str(path), events=("end",), huge_tree=True, recover=True):
            tag = etree.QName(el).localname
            if tag == "nameAlias":
                w = el.get("wholeName") or el.get("aliasName")
                if w and len(w) > 2:
                    names.append(w.strip())
                fn = el.get("firstName")
                ln = el.get("lastName")
                if fn or ln:
                    names.append(" ".join(x for x in (fn, ln) if x).strip())
            el.clear()
    except Exception as e:
        logger.warning("EU XML parse error: %s", e)
    return names


def _parse_ofac_names(path: Path) -> list[str]:
    names: list[str] = []
    if not path.exists():
        return names
    try:
        for _, el in etree.iterparse(str(path), events=("end",), huge_tree=True, recover=True):
            if etree.QName(el).localname != "sdnEntry":
                el.clear()
                continue
            fn, ln = "", ""
            for ch in el:
                tag = etree.QName(ch).localname
                if tag == "firstName":
                    fn = (ch.text or "").strip()
                elif tag == "lastName":
                    ln = (ch.text or "").strip()
                elif tag == "programList" and ch.text:
                    t = ch.text.strip()
                    if len(t) > 2:
                        names.append(t)
            if fn or ln:
                names.append(f"{fn} {ln}".strip())
            el.clear()
    except Exception as e:
        logger.warning("OFAC XML parse error: %s", e)
    return names


def _parse_un_names(path: Path) -> list[str]:
    names: list[str] = []
    if not path.exists():
        return names
    try:
        for _, el in etree.iterparse(str(path), events=("end",), huge_tree=True, recover=True):
            tag = etree.QName(el).localname
            if tag in ("INDIVIDUAL", "ENTITY", "Individual", "Entity"):
                for ch in el.iter():
                    ct = etree.QName(ch).localname
                    if ct in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "NAME", "ENTITY_NAME"):
                        t = (ch.text or "").strip()
                        if len(t) > 2:
                            names.append(t)
            el.clear()
    except Exception as e:
        logger.warning("UN XML parse error: %s", e)
    return names


def _parse_mswia_names(path: Path) -> list[str]:
    names: list[str] = []
    if not path.exists():
        return names
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "lxml")
        for row in soup.select("table tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            for c in cells:
                if len(c) > 5 and re.search(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]", c):
                    names.append(c)
    except Exception as e:
        logger.warning("MSWiA HTML parse error: %s", e)
    return names


class SanctionsIndex:
    """In-memory index rebuilt after cache refresh."""

    _choices: list[str] | None = None
    _built_at: float = 0.0

    @classmethod
    def choices(cls) -> list[str]:
        if cls._choices is not None and (time.time() - cls._built_at) < 60:
            return cls._choices
        ensure_sanctions_cache()
        base = _cache_dir()
        all_n: list[str] = []
        all_n.extend(_parse_eu_names(base / CACHE_FILES["eu"]))
        all_n.extend(_parse_ofac_names(base / CACHE_FILES["ofac"]))
        all_n.extend(_parse_un_names(base / CACHE_FILES["un"]))
        all_n.extend(_parse_mswia_names(base / CACHE_FILES["mswia"]))
        seen: set[str] = set()
        dedup: list[str] = []
        for n in all_n:
            k = n.lower().strip()
            if k not in seen and len(k) > 2:
                seen.add(k)
                dedup.append(n.strip())
        cls._choices = dedup
        cls._built_at = time.time()
        logger.info("Sanctions index built: %d unique names", len(dedup))
        return cls._choices

    @classmethod
    def invalidate(cls) -> None:
        cls._choices = None


def fuzzy_match_one(query: str, choices: Sequence[str] | None = None) -> Optional[tuple[str, float]]:
    if not query or len(query.strip()) < 2:
        return None
    ch = list(choices) if choices is not None else SanctionsIndex.choices()
    if not ch:
        return None
    hit = process.extractOne(
        query.strip(),
        ch,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=85,
    )
    if not hit:
        return None
    name, score, _ = hit
    return name, float(score)


def check_names(
    *,
    company_names: Iterable[str],
    person_names: Iterable[str],
) -> list[SanctionsMatch]:
    """Check company + person strings against all lists (treated as one combined index)."""
    ensure_sanctions_cache()
    ch = SanctionsIndex.choices()
    out: list[SanctionsMatch] = []
    for nm in company_names:
        if not nm:
            continue
        hit = fuzzy_match_one(nm, ch)
        if hit:
            ent, sc = hit
            out.append(
                SanctionsMatch(
                    matched=True,
                    match_score=sc,
                    matched_entity=ent,
                    list_name="EU/OFAC/UN/PL (combined index)",
                    match_type="company",
                )
            )
    for nm in person_names:
        if not nm:
            continue
        hit = fuzzy_match_one(nm, ch)
        if hit:
            ent, sc = hit
            out.append(
                SanctionsMatch(
                    matched=True,
                    match_score=sc,
                    matched_entity=ent,
                    list_name="EU/OFAC/UN/PL (combined index)",
                    match_type="person",
                )
            )
    return out

