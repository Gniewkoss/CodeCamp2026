from __future__ import annotations

import re
from functools import lru_cache

from textblob import TextBlob
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_vader = SentimentIntensityAnalyzer()


@lru_cache(maxsize=1)
def _pl_pipeline():
    """Polish-capable transformer sentiment (multilingual RoBERTa; HerBERT has no public sentiment head)."""
    try:
        from transformers import pipeline

        return pipeline(
            "sentiment-analysis",
            model="cardiffnlp/twitter-xlm-roberta-base-sentiment",
            tokenizer="cardiffnlp/twitter-xlm-roberta-base-sentiment",
            truncation=True,
            max_length=512,
        )
    except Exception:
        return None


def _compound_to_neg_pos(compound: float) -> float:
    return max(-1.0, min(1.0, compound))


def score_english_vader(text: str) -> float:
    if not text or not text.strip():
        return 0.0
    scores = _vader.polarity_scores(text)
    return _compound_to_neg_pos(scores["compound"])


def score_english_textblob(text: str) -> float:
    if not text or not text.strip():
        return 0.0
    polarity = TextBlob(text).sentiment.polarity
    return max(-1.0, min(1.0, polarity))


def score_polish_transformers(text: str) -> float:
    if not text or not text.strip():
        return 0.0
    pipe = _pl_pipeline()
    if pipe is None:
        return score_english_vader(text)
    chunk = re.sub(r"\s+", " ", text)[:4000]
    try:
        out = pipe(chunk[:2000])[0]
        label = str(out.get("label", "")).upper()
        s = float(out.get("score", 0.5))
        if "NEG" in label or label == "LABEL_0":
            return max(-1.0, min(0.0, -s))
        if "POS" in label or label == "LABEL_2":
            return max(0.0, min(1.0, s))
        return 0.0
    except Exception:
        return score_english_vader(chunk)


def sentiment_for_text(text: str, language: str | None) -> float:
    lang = (language or "pl").lower()
    if lang.startswith("en"):
        return score_english_vader(text)
    return score_polish_transformers(text)
