"""Sanctions list download, cache (24h), and fuzzy name matching.

Data source: **OpenSanctions** consolidated CSV.
https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv

OpenSanctions is a professional KYC aggregator that unifies every major
public sanctions regime — EU, OFAC SDN, UN, UK OFSI, Swiss SECO, Polish
MSWiA, Ukrainian NSDC, Canada, Australia, and 100+ others — into a single
normalised CSV that refreshes daily. It's free for bulk download (no API
key required) under the CC-BY 4.0 license.

Swapping our legacy EU/OFAC/UN/MSWiA parsers for this one feed gives us:
* **Higher coverage** — one consolidated list instead of four that we had
  to scrape separately (EU webgate requires auth, MSWiA is HTML, etc.).
* **Aliases + transliterations** — each entity ships with Cyrillic,
  Latin, and native-script spellings of the same name, which is crucial
  for matching Russian/Ukrainian companies in Polish news.
* **Program metadata** — ``dataset`` + ``sanctions`` columns tell us
  *why* someone is on the list (EU RU, OFAC RUSSIA-EO14024, UN 1267, …)
  so we can cite the regime in ``SanctionsMatch.list_name``.
* **Zero registration** — the old EU FSF XML at webgate.ec.europa.eu now
  returns 403 Forbidden for public callers.

Uses rapidfuzz.fuzz.token_sort_ratio with cutoff 85 (spec).
Caches the CSV + a pre-normalised name index under
``settings.sanctions_cache_dir`` (e.g. ./data/sanctions).
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import httpx
from rapidfuzz import fuzz, process

from app.config import get_settings

logger = logging.getLogger(__name__)

# Main data feed — ~60 MB CSV, refreshed daily on OpenSanctions CDN.
OPENSANCTIONS_CSV_URL = (
    "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"
)

CACHE_FILES = {
    "opensanctions": "opensanctions.csv",
    "index": "opensanctions_index.json",
}
META_FILE = "meta.json"
REFRESH_SEC = 24 * 3600

# Lift the CSV parser's default 128k field cap — OpenSanctions occasionally
# has rows with very long alias strings (dozens of transliterations), and
# hitting the limit silently truncates the row.
csv.field_size_limit(10_000_000)


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
    with httpx.Client(timeout=300.0, follow_redirects=True) as client:
        with client.stream(
            "GET", url,
            headers={"User-Agent": "ReputationMonitor/2.0 (+OpenSanctions consumer)"},
        ) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
            tmp.replace(dest)


def ensure_sanctions_cache() -> None:
    """Download / refresh OpenSanctions CSV if older than 24h."""
    base = _cache_dir()
    meta = _load_meta()
    now = datetime.now(timezone.utc).isoformat()

    csv_path = base / CACHE_FILES["opensanctions"]
    if _needs_refresh("opensanctions") or not csv_path.exists():
        try:
            logger.info("Downloading OpenSanctions CSV (~60 MB) → %s", csv_path.name)
            _download(OPENSANCTIONS_CSV_URL, csv_path)
            meta["opensanctions"] = now
            # Force re-indexing after a fresh download.
            idx = base / CACHE_FILES["index"]
            if idx.exists():
                try:
                    idx.unlink()
                except OSError:
                    pass
            _save_meta(meta)
            SanctionsIndex.invalidate()
        except Exception as e:
            logger.warning("OpenSanctions download failed: %s", e)


# ────────────────────────────────────────────────────────────────────────
# Parsing
# ────────────────────────────────────────────────────────────────────────


# ``schema`` values in the OpenSanctions simple export. We map anything
# "organisational" to "company" and anything personal to "person"; that
# matches the way downstream callers key their logic.
_PERSON_SCHEMAS = {"Person", "LegalEntity"}  # LegalEntity is the abstract base but rare in simple export
_COMPANY_SCHEMAS = {
    "Company", "Organization", "PublicBody", "Airline", "Fund",
    "Project", "Trust", "Vessel",
}


def _split_aliases(raw: str) -> list[str]:
    """OpenSanctions packs aliases as ``;``-separated strings. CSV already
    handled outer-quote escaping, so we just split and trim."""
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(";"):
        part = part.strip().strip('"')
        if len(part) >= 3:
            out.append(part)
    return out


@dataclass
class _IndexEntry:
    name: str                 # canonical display name
    kind: str                 # "person" | "company"
    list_tag: str             # short provenance, e.g. "EU RU / OFAC SDN"
    program_ids: str          # comma-sep program IDs for traceability


def _parse_opensanctions(path: Path) -> list[_IndexEntry]:
    """Stream the CSV and return a flat list of index entries (1 per name/alias)."""
    entries: list[_IndexEntry] = []
    if not path.exists():
        return entries

    row_count = 0
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                row_count += 1
                schema = (row.get("schema") or "").strip()
                name = (row.get("name") or "").strip()
                if not name:
                    continue

                if schema in _COMPANY_SCHEMAS:
                    kind = "company"
                elif schema == "Person":
                    kind = "person"
                else:
                    # Unknown schema → best-effort heuristic: if aliases look
                    # like a company (contain "Ltd"/"GmbH"/"Sp. z o.o."), lean
                    # company; else person. Rarely matters in practice.
                    kind = "company" if any(
                        t in name.lower() for t in
                        (" ltd", " gmbh", " sa", " sp.", " ooo", " oao", " pao")
                    ) else "person"

                datasets = (row.get("dataset") or "").strip()
                list_tag = _short_list_tag(datasets)
                program_ids = (row.get("program_ids") or "").strip()

                entries.append(_IndexEntry(
                    name=name, kind=kind,
                    list_tag=list_tag, program_ids=program_ids,
                ))

                # Expand aliases so fuzzy matching catches transliterations.
                for alias in _split_aliases(row.get("aliases") or ""):
                    entries.append(_IndexEntry(
                        name=alias, kind=kind,
                        list_tag=list_tag, program_ids=program_ids,
                    ))
    except Exception as e:
        logger.warning("OpenSanctions CSV parse error at row ~%d: %s", row_count, e)
    logger.info(
        "OpenSanctions CSV parsed: %d rows → %d raw name entries",
        row_count, len(entries),
    )
    return entries


def _short_list_tag(datasets_raw: str) -> str:
    """Collapse the full dataset name list into a short provenance tag.

    ``"US OFAC Specially Designated Nationals (SDN) List;US Trade Consolidated Screening List (CSL)"``
    becomes ``"OFAC SDN / US CSL"``.
    """
    if not datasets_raw:
        return "OpenSanctions"
    tags: list[str] = []
    for ds in datasets_raw.split(";"):
        ds = ds.strip()
        if not ds:
            continue
        low = ds.lower()
        if "ofac" in low or "sdn" in low:
            tags.append("OFAC SDN")
        elif "consolidated screening" in low or "csl" in low:
            tags.append("US CSL")
        elif "eu financial sanctions" in low or "eu consolidated" in low or "council of the eu" in low:
            tags.append("EU FSF")
        elif "uk ofsi" in low or "hm treasury" in low:
            tags.append("UK OFSI")
        elif "un security council" in low or "un consolidated" in low or "un 1267" in low:
            tags.append("UN")
        elif "swiss seco" in low or "swiss federal" in low:
            tags.append("CH SECO")
        elif "mswia" in low or "polish " in low or "poland " in low:
            tags.append("PL MSWiA")
        elif "ukraine" in low or "nsdc" in low:
            tags.append("UA NSDC")
        elif "canada" in low:
            tags.append("CA")
        elif "australia" in low or "dfat" in low:
            tags.append("AU DFAT")
        elif "interpol" in low:
            tags.append("Interpol")
        else:
            tags.append(ds[:40])
    # Dedupe in insertion order.
    seen: set[str] = set()
    out = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return " / ".join(out) or "OpenSanctions"


# ────────────────────────────────────────────────────────────────────────
# Index
# ────────────────────────────────────────────────────────────────────────


class SanctionsIndex:
    """In-memory index rebuilt after cache refresh.

    Holds a flat list of normalised display names (for fuzzy match) plus a
    parallel side-table with ``(kind, list_tag, program_ids)`` metadata so
    ``check_names`` can report *which* regime flagged each hit.
    """

    _choices: list[str] | None = None
    _meta: dict[str, _IndexEntry] | None = None
    _built_at: float = 0.0

    @classmethod
    def _build(cls) -> None:
        ensure_sanctions_cache()
        csv_path = _cache_dir() / CACHE_FILES["opensanctions"]
        entries = _parse_opensanctions(csv_path)

        seen: dict[str, _IndexEntry] = {}
        choices: list[str] = []
        for e in entries:
            key = e.name.lower().strip()
            if len(key) < 3:
                continue
            if key in seen:
                # Merge list_tags when the same alias appears on multiple
                # regimes (e.g. the same Russian oligarch on EU + OFAC).
                # Cap at 4 regimes to keep UI labels readable — the fact
                # that someone is on 15 sanctions lists doesn't add useful
                # signal over "EU FSF / OFAC SDN / UK OFSI / …".
                prev = seen[key]
                if e.list_tag and e.list_tag not in prev.list_tag:
                    current_count = prev.list_tag.count(" / ") + 1
                    if current_count < 4:
                        prev.list_tag = f"{prev.list_tag} / {e.list_tag}"
                    elif "…" not in prev.list_tag:
                        prev.list_tag = f"{prev.list_tag} / …"
                continue
            seen[key] = e
            choices.append(e.name.strip())

        cls._choices = choices
        cls._meta = seen
        cls._built_at = time.time()
        logger.info("Sanctions index built: %d unique names", len(choices))

    @classmethod
    def choices(cls) -> list[str]:
        if cls._choices is not None and (time.time() - cls._built_at) < 300:
            return cls._choices
        cls._build()
        return cls._choices or []

    @classmethod
    def lookup(cls, name: str) -> Optional[_IndexEntry]:
        """Return the metadata row for a previously-matched canonical name."""
        if cls._meta is None:
            cls._build()
        if cls._meta is None:
            return None
        return cls._meta.get(name.lower().strip())

    @classmethod
    def invalidate(cls) -> None:
        cls._choices = None
        cls._meta = None


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
    """Check company + person strings against the OpenSanctions index."""
    ensure_sanctions_cache()
    ch = SanctionsIndex.choices()
    out: list[SanctionsMatch] = []

    def _match(nm: str, expected_kind: str) -> Optional[SanctionsMatch]:
        if not nm:
            return None
        hit = fuzzy_match_one(nm, ch)
        if not hit:
            return None
        ent, sc = hit
        meta = SanctionsIndex.lookup(ent)
        # Prefer the matched entity's actual kind over the caller's guess —
        # if we matched a company name to a person list-entry or vice versa
        # (rare), the provenance tag still keeps the finding auditable.
        kind = meta.kind if meta else expected_kind
        tag = meta.list_tag if meta else "OpenSanctions"
        return SanctionsMatch(
            matched=True,
            match_score=sc,
            matched_entity=ent,
            list_name=tag,
            match_type=kind,
        )

    for nm in company_names:
        m = _match(nm, "company")
        if m:
            out.append(m)
    for nm in person_names:
        m = _match(nm, "person")
        if m:
            out.append(m)
    return out
