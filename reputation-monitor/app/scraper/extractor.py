"""Lightweight article text extractor — httpx + BeautifulSoup."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 ReputationMonitor/2.0"
)


@dataclass
class ExtractedArticle:
    title: str | None
    text: str | None
    language: str | None


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return "unknown"


_ARTICLE_SELECTORS = (
    "article",
    "main",
    '[role="main"]',
    ".article-body",
    ".article__body",
    ".articleBody",
    ".post-content",
    ".entry-content",
    ".content",
    "#article",
)


def fetch_article_text(url: str, *, timeout: float = 20.0) -> ExtractedArticle:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "pl,en;q=0.8"}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        logger.debug("fetch failed for %s: %s", url, e)
        return ExtractedArticle(title=None, text=None, language=None)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()

    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe"]):
        tag.decompose()

    best_text = ""
    for sel in _ARTICLE_SELECTORS:
        node = soup.select_one(sel)
        if not node:
            continue
        t = node.get_text(separator="\n", strip=True)
        if len(t) > len(best_text):
            best_text = t
        if len(best_text) > 600:
            break

    if len(best_text) < 200 and soup.body:
        best_text = soup.body.get_text(separator="\n", strip=True)

    text = re.sub(r"\n{3,}", "\n\n", best_text).strip()
    if len(text) < 40:
        text = ""

    language = None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        language = html_tag["lang"][:8]

    return ExtractedArticle(title=title, text=text[:60000] or None, language=language)


def infer_language(text: str | None) -> str:
    if not text:
        return "pl"
    sample = text[:2000].lower()
    if any(ch in sample for ch in "ąęłńóśźżć"):
        return "pl"
    return "en"
