"""Client for the Polish Portal Rejestrów Sądowych (PRS) financial-statement
browser — https://rdf-przegladarka.ms.gov.pl/.

Background
----------
The legacy KRS RDF portal at ``ekrs.ms.gov.pl/rdf/`` used XHTML-over-JSF and
our ``polite_list_statements`` scraper against it: it has been decommissioned
and the URL now redirects to a read-only info page. The replacement is a
React-style Angular SPA hosted at ``rdf-przegladarka.ms.gov.pl`` that talks
to a small REST API living on the same host. That API is what we call from
Python here — no browser/Playwright needed in production, despite the SPA
front-end.

The flow is:

1. ``POST /podmioty/wyszukiwanie/dane-podstawowe`` with ``{"numerKRS": "..."}``
   returns basic subject info (name, form, status) and confirms the KRS
   number resolves.

2. ``POST /dokumenty/wyszukiwanie`` with a **client-encrypted** ``nrKRS``
   returns the paginated list of filed documents (``id``, ``rodzaj``, period,
   status). The encryption is a two-round AES-CBC-Pkcs7 ("inner" with a
   random 16-digit numeric key, "outer" with a hard-coded 16-byte permKey
   baked into the SPA's main JS bundle: ``"6a5Qm4W&MkiD=hwo"``).

3. ``GET /dokumenty/{idEncrypted}/tresc`` returns the raw file blob. The
   portal stores rendered documents as XHTML (from ``pdf2htmlEX``) and
   structured JPK_SF / e-SF XML depending on the *rodzaj*. Either way the
   content is text — we extract whatever we can and hand it to
   ``extract_from_text`` (LLM) for structured figure extraction.

Caching
-------
Downloaded document blobs are cached under
``settings.sanctions_cache_dir.parent / "prs_rdf"`` keyed by KRS + doc id so
re-runs of the same company on the same day never re-hit the portal.

Rate limiting
-------------
The SPA self-throttles to 3 POST ``/dokumenty/wyszukiwanie`` calls per 5
seconds; we replicate that with a simple token bucket to stay well-mannered
and avoid IP bans.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from app.config import get_settings

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

_BASE = "https://rdf-przegladarka.ms.gov.pl/services/rdf/przegladarka-dokumentow-finansowych"
_ORIGIN = "https://rdf-przegladarka.ms.gov.pl"
_PERM_KEY = b"6a5Qm4W&MkiD=hwo"  # baked into main-*.js encryptNrKrs()

_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "pl-PL,pl;q=0.9",
    "Origin": _ORIGIN,
    "Referer": f"{_ORIGIN}/wyszukaj-podmiot",
}

# Rodzaj codes → human readable (reverse-engineered from the portal's
# RodzajeDokumentuItems enum that the SPA populates on form open).
_RODZAJ_LABEL = {
    "3": "Roczne sprawozdanie finansowe",
    "4": "Sprawozdanie z działalności",
    "9": "Uchwała o zatwierdzeniu sprawozdania",
    "18": "Skonsolidowane roczne sprawozdanie finansowe",
    "19": "Skonsolidowane sprawozdanie z działalności",
    "25": "Sprawozdanie z badania (opinia biegłego)",
    "26": "Sprawozdanie z atestacji / ESG",
    "27": "Uchwała o podziale zysku / pokryciu straty",
}

# Which rodzaj codes contain actual balance-sheet / income-statement figures
# worth feeding to the extractor (vs. audit opinions, resolutions, …).
_FINANCIAL_RODZAJ = {"3", "4", "18", "19"}


# ────────────────────────────────────────────────────────────────────────
# Rate limiter — 3 req / 5 s (matches the SPA's own anti-abuse window)
# ────────────────────────────────────────────────────────────────────────


class _TokenBucket:
    def __init__(self, max_requests: int = 3, window_seconds: float = 5.0) -> None:
        self.max = max_requests
        self.window = window_seconds
        self._times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                # Drop timestamps outside the window.
                self._times = [t for t in self._times if (now - t) < self.window]
                if len(self._times) < self.max:
                    self._times.append(now)
                    return
                sleep_for = self.window - (now - self._times[0]) + 0.05
            # Sleep *outside* the lock so other threads can still update state.
            if sleep_for > 0:
                time.sleep(sleep_for)


_GLOBAL_BUCKET = _TokenBucket()


# ────────────────────────────────────────────────────────────────────────
# AES encryption of KRS number (mirrors the SPA's encryptNrKrs())
# ────────────────────────────────────────────────────────────────────────


def _aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def _encrypt_krs(krs: str) -> str:
    """Return the SPA-compatible, base64 encrypted form of a KRS number."""
    padded = krs.zfill(10).encode("utf-8")
    # Random 16-digit numeric string → used as inner key AND iv (matches SPA).
    rand_num = str(int.from_bytes(os.urandom(4), "big")).zfill(16)[:16]
    inner = _aes_cbc_encrypt(padded, rand_num.encode(), rand_num.encode())
    inner_b64 = base64.b64encode(inner).decode("ascii")
    outer = _aes_cbc_encrypt(
        f"{inner_b64}.{rand_num}".encode("utf-8"),
        _PERM_KEY, _PERM_KEY,
    )
    return base64.b64encode(outer).decode("ascii")


# ────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass
class PRSDocument:
    id: str                       # SPA-encrypted ID (pass as path param)
    rodzaj: str                   # code, e.g. "3"
    rodzaj_label: str             # human readable
    period_start: str             # "YYYY-MM-DD" (pustka jeśli brak)
    period_end: str
    status: str
    is_financial: bool            # True iff rodzaj ∈ _FINANCIAL_RODZAJ


@dataclass
class PRSFetchResult:
    """What ``fetch_financial_documents`` returns for one KRS."""
    podmiot_name: str = ""
    forma_prawna: str = ""
    documents: list[PRSDocument] = field(default_factory=list)
    text_by_period: dict[str, str] = field(default_factory=dict)
    # Periods we have *some* extractable text for, newest first.
    periods_with_text: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ────────────────────────────────────────────────────────────────────────
# Cache
# ────────────────────────────────────────────────────────────────────────


def _cache_root() -> Path:
    # Re-use the sanctions cache parent so all MS-Gov artefacts live together.
    base = Path(get_settings().sanctions_cache_dir).parent / "prs_rdf"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cache_path(krs: str, doc_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", doc_id)[:60]
    return _cache_root() / f"{krs}__{safe_id}.bin"


# ────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ────────────────────────────────────────────────────────────────────────


def _client() -> httpx.Client:
    return httpx.Client(
        headers=_HEADERS_BASE,
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0),
        follow_redirects=True,
    )


def _post_json(client: httpx.Client, path: str, body: dict) -> dict | list:
    _GLOBAL_BUCKET.acquire()
    r = client.post(
        f"{_BASE}{path}",
        headers={"Content-Type": "application/json"},
        json=body,
    )
    r.raise_for_status()
    return r.json()


def _get(client: httpx.Client, path: str, accept: str = "application/json") -> httpx.Response:
    _GLOBAL_BUCKET.acquire()
    r = client.get(f"{_BASE}{path}", headers={"Accept": accept})
    r.raise_for_status()
    return r


# ────────────────────────────────────────────────────────────────────────
# Public: list podmiot + documents
# ────────────────────────────────────────────────────────────────────────


def find_podmiot(krs: str, *, client: httpx.Client | None = None) -> Optional[dict]:
    """Return the basic podmiot block or None if PRS cannot resolve KRS."""
    krs = krs.strip().zfill(10)
    if not krs.isdigit() or len(krs) != 10:
        return None
    _own = client is None
    cli = client or _client()
    try:
        data = _post_json(cli, "/podmioty/wyszukiwanie/dane-podstawowe", {"numerKRS": krs})
        if not isinstance(data, dict):
            return None
        if not data.get("czyPodmiotZnaleziony"):
            return None
        return data.get("podmiot") or None
    except httpx.HTTPError as e:
        logger.info("PRS podmiot lookup failed for %s: %s", krs, e)
        return None
    finally:
        if _own:
            cli.close()


def list_documents(
    krs: str,
    *,
    page_size: int = 30,
    max_pages: int = 3,
    client: httpx.Client | None = None,
) -> list[PRSDocument]:
    """Return filed documents, newest first."""
    krs = krs.strip().zfill(10)
    _own = client is None
    cli = client or _client()
    out: list[PRSDocument] = []
    try:
        nr_encrypted = _encrypt_krs(krs)
        for page in range(max_pages):
            body = {
                "metadaneStronicowania": {
                    "numerStrony": page,
                    "rozmiarStrony": page_size,
                    "metadaneSortowania": [{"atrybut": "id", "kierunek": "MALEJACO"}],
                },
                "nrKRS": nr_encrypted,
            }
            try:
                data = _post_json(cli, "/dokumenty/wyszukiwanie", body)
            except httpx.HTTPStatusError as e:
                # 422 means the server rejected our decryption — re-encrypt
                # once before giving up (the random component sometimes
                # yields a ciphertext the server dislikes; brand new attempt
                # usually works).
                if e.response.status_code == 422 and page == 0:
                    nr_encrypted = _encrypt_krs(krs)
                    data = _post_json(cli, "/dokumenty/wyszukiwanie", body)
                else:
                    raise
            if not isinstance(data, dict):
                break
            content = data.get("content") or []
            if not content:
                break
            for d in content:
                if d.get("dataUsunieciaDokumentu"):
                    # Hidden / withdrawn filings — skip.
                    continue
                rodzaj = str(d.get("rodzaj") or "").strip()
                out.append(PRSDocument(
                    id=str(d.get("id") or ""),
                    rodzaj=rodzaj,
                    rodzaj_label=_RODZAJ_LABEL.get(rodzaj, f"Rodzaj {rodzaj}"),
                    period_start=str(d.get("okresSprawozdawczyPoczatek") or ""),
                    period_end=str(d.get("okresSprawozdawczyKoniec") or ""),
                    status=str(d.get("status") or ""),
                    is_financial=rodzaj in _FINANCIAL_RODZAJ,
                ))
            if len(content) < page_size:
                break
    except httpx.HTTPError as e:
        logger.info("PRS list_documents failed for %s: %s", krs, e)
    finally:
        if _own:
            cli.close()
    return out


# ────────────────────────────────────────────────────────────────────────
# Download + text extraction
# ────────────────────────────────────────────────────────────────────────


def download_document_raw(
    krs: str,
    doc: PRSDocument,
    *,
    client: httpx.Client | None = None,
    use_cache: bool = True,
) -> bytes:
    """Download a document body (XHTML / XML / PDF) — cached on disk."""
    cache = _cache_path(krs, doc.id)
    if use_cache and cache.exists():
        try:
            return cache.read_bytes()
        except OSError:
            pass

    _own = client is None
    cli = client or _client()
    try:
        enc_id = urllib.parse.quote(doc.id, safe="")
        r = _get(cli, f"/dokumenty/{enc_id}/tresc", accept="application/octet-stream")
        data = r.content
        try:
            cache.write_bytes(data)
        except OSError as e:
            logger.debug("PRS cache write failed: %s", e)
        return data
    finally:
        if _own:
            cli.close()


_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _html_to_text(data: bytes) -> str:
    """Strip CSS/HTML tags from the XHTML output of pdf2htmlEX."""
    try:
        import warnings

        from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    except ImportError:
        return data.decode("utf-8", errors="ignore")

    # PRS financial XHTML often has an XML prolog that makes BS4 whine when
    # parsed with the HTML parser. That's fine for our "strip to text" use
    # case — squelch the warning instead of spamming the logs.
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

    try:
        soup = BeautifulSoup(data, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(data, "html.parser")
        except Exception:
            return data.decode("utf-8", errors="ignore")

    for bad in soup.find_all(["style", "script", "noscript", "svg", "meta", "link"]):
        bad.decompose()
    text = soup.get_text("\n", strip=True)
    # Collapse runs of blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _WS_RE.sub(" ", text)
    return text


def _xml_to_text(data: bytes) -> str:
    """Render JPK_SF / e-SF XML as a key: value list for easy LLM parsing."""
    try:
        from lxml import etree
    except ImportError:
        return data.decode("utf-8", errors="ignore")
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(huge_tree=True, recover=True))
    except Exception:
        return data.decode("utf-8", errors="ignore")

    lines: list[str] = []
    for el in root.iter():
        tag = etree.QName(el).localname
        txt = (el.text or "").strip()
        if txt and len(txt) < 400:
            lines.append(f"{tag}: {txt}")
    return "\n".join(lines)


def _pdf_to_text(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:
        logger.debug("pdfplumber not available; skipping PDF extraction")
        return ""
    from io import BytesIO
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            chunks = [p.extract_text() or "" for p in pdf.pages]
        return "\n".join(chunks)
    except Exception as e:
        logger.info("PDF parse failed: %s", e)
        return ""


def document_to_text(data: bytes) -> str:
    """Dispatch XHTML / XML / PDF to the right extractor."""
    if not data:
        return ""
    head = data[:256].lstrip().lower()
    if head.startswith(b"%pdf"):
        return _pdf_to_text(data)
    if head.startswith(b"<?xml") and b"xhtml" not in head and b"<html" not in head:
        return _xml_to_text(data)
    if b"<html" in head or b"xhtml" in head or head.startswith(b"<!doctype"):
        return _html_to_text(data)
    # Plain text fallback.
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


# ────────────────────────────────────────────────────────────────────────
# Public: high-level "give me N years of statements" orchestration
# ────────────────────────────────────────────────────────────────────────


def fetch_financial_documents(
    krs: str,
    *,
    years: int = 3,
    include_consolidated: bool = True,
) -> PRSFetchResult:
    """Download and extract text for up to ``years`` most recent filings.

    Returns combined text per ``period_end`` (useful for ``extract_from_text``).
    Graceful on every axis: missing podmiot, network errors, rate limiting.
    """
    krs = (krs or "").strip().zfill(10)
    result = PRSFetchResult()
    if not krs.isdigit() or len(krs) != 10:
        result.error = f"invalid KRS '{krs}'"
        return result

    try:
        with _client() as cli:
            podmiot = find_podmiot(krs, client=cli)
            if not podmiot:
                result.error = "podmiot nie został znaleziony w PRS"
                return result
            result.podmiot_name = str(podmiot.get("nazwaPodmiotu") or "")
            result.forma_prawna = str(podmiot.get("formaPrawna") or "")

            docs = list_documents(krs, client=cli)
            if not docs:
                result.error = "brak dokumentów w RDF"
                return result

            # Pick docs worth reading. Strategy:
            # * Only rodzaje with real financial content.
            # * Group by period_end (latest first), cap to ``years``.
            # * If ``include_consolidated`` is False, drop rodzaj 18/19.
            financial_docs = [
                d for d in docs
                if d.is_financial
                and (include_consolidated or d.rodzaj in {"3", "4"})
                and d.period_end
            ]
            if not financial_docs:
                # Fall back to anything with a period end so we at least give
                # the LLM something to chew on (audit opinions etc.).
                financial_docs = [d for d in docs if d.period_end][:years * 2]
            # Newest periods first.
            financial_docs.sort(key=lambda d: d.period_end, reverse=True)

            seen_periods: list[str] = []
            for d in financial_docs:
                if d.period_end in seen_periods:
                    # Combine multiple rodzaj docs for the same period into
                    # one text block further down.
                    pass
                elif len(seen_periods) >= years:
                    break
                else:
                    seen_periods.append(d.period_end)

            # Download + transcribe only the docs for selected periods.
            for d in financial_docs:
                if d.period_end not in seen_periods:
                    continue
                try:
                    blob = download_document_raw(krs, d, client=cli)
                except httpx.HTTPError as e:
                    logger.info("PRS download failed (krs=%s id=%s): %s", krs, d.id[:12], e)
                    continue
                text = document_to_text(blob)
                if not text:
                    continue
                prev = result.text_by_period.get(d.period_end, "")
                header = f"=== {d.rodzaj_label} ({d.period_start} – {d.period_end}) ===\n"
                result.text_by_period[d.period_end] = (prev + "\n\n" if prev else "") + header + text

            result.documents = docs
            result.periods_with_text = sorted(
                result.text_by_period.keys(), reverse=True
            )
    except httpx.HTTPError as e:
        result.error = f"HTTP error: {e}"
    except Exception as e:
        logger.exception("PRS fetch unexpected error: %s", e)
        result.error = f"{type(e).__name__}: {e}"
    return result
