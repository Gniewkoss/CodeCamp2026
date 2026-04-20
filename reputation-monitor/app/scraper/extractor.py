from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class ExtractedArticle:
    title: str | None
    text: str | None
    language: str | None


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return "unknown"


def extract_with_newspaper(url: str) -> ExtractedArticle | None:
    try:
        from newspaper import Article

        art = Article(url, language="pl")
        art.download()
        art.parse()
        return ExtractedArticle(title=art.title, text=art.text, language=art.meta_lang)
    except Exception as e:
        logger.debug("newspaper failed for %s: %s", url, e)
        return None


def extract_with_bs4(url: str, timeout: float = 25.0) -> ExtractedArticle | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        title = None
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        parts: list[str] = []
        for sel in ("article", "main", '[role="main"]', ".article-body", ".article__body"):
            node = soup.select_one(sel)
            if node:
                t = node.get_text(separator="\n", strip=True)
                if len(t) > 200:
                    parts.append(t)
                    break
        if not parts:
            body = soup.body or soup
            parts.append(body.get_text(separator="\n", strip=True))
        text = "\n".join(parts).strip()
        if len(text) < 80:
            return None
        return ExtractedArticle(title=title, text=text[:500_000], language=None)
    except Exception as e:
        logger.debug("bs4 extract failed for %s: %s", url, e)
        return None


async def extract_with_playwright(url: str) -> ExtractedArticle | None:
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            title = await page.title()
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            await browser.close()
        text = (text or "").strip()
        if len(text) < 80:
            return None
        return ExtractedArticle(title=title, text=text[:500_000], language=None)
    except Exception as e:
        logger.debug("playwright extract failed for %s: %s", url, e)
        return None


def fetch_article_text(url: str, *, use_playwright: bool = False) -> ExtractedArticle:
    ex = extract_with_newspaper(url)
    if ex and ex.text and len(ex.text) > 80:
        return ex
    ex = extract_with_bs4(url)
    if ex and ex.text:
        return ex
    if use_playwright:
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        pw = loop.run_until_complete(extract_with_playwright(url))
        if pw and pw.text:
            return pw
    return ex or ExtractedArticle(title=None, text=None, language=None)


def infer_language(text: str | None) -> str:
    if not text:
        return "pl"
    sample = text[:2000].lower()
    pl_markers = ("ą", "ę", "ł", "ń", "ó", "ś", "ź", "ż", "ć", " i ", " w ", " na ")
    if any(m in sample for m in pl_markers if len(m) == 1) or " w " in f" {sample} ":
        return "pl"
    return "en"
