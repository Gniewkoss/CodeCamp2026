from __future__ import annotations

from dataclasses import dataclass

RISK_KEYWORDS: dict[str, dict] = {
    "corruption": {
        "keywords": ["korupcja", "łapówka", "przekupstwo", "bribery", "corruption", "CBA", "ABW"],
        "weight": 10,
    },
    "legal": {
        "keywords": [
            "zarzuty",
            "prokuratura",
            "areszt",
            "sąd",
            "wyrok",
            "pozew",
            "charges",
            "indicted",
            "arrested",
        ],
        "weight": 8,
    },
    "management": {
        "keywords": [
            "rezygnacja zarządu",
            "odwołanie",
            "zwolnienie prezesa",
            "CEO resignation",
            "fired",
        ],
        "weight": 5,
    },
    "sanctions": {
        "keywords": ["sankcje", "sanctions", "OFAC", "blacklist", "czarna lista", "embargo"],
        "weight": 9,
    },
    "financial": {
        "keywords": [
            "upadłość",
            "bankructwo",
            "windykacja",
            "niewypłacalność",
            "bankruptcy",
            "insolvency",
            "fraud",
        ],
        "weight": 7,
    },
    "regulatory": {
        "keywords": [
            "KNF",
            "UOKiK",
            "kara",
            "naruszenie",
            "nadzór",
            "investigation",
            "fine",
            "penalty",
        ],
        "weight": 6,
    },
}


@dataclass
class KeywordMatch:
    category: str
    keyword: str
    weight: int


def match_risk_keywords(text: str) -> list[KeywordMatch]:
    """Case-insensitive substring scan for lexicon hits (longer phrases first per category)."""
    if not text:
        return []
    lowered = text.lower()
    hits: list[KeywordMatch] = []
    for category, spec in RISK_KEYWORDS.items():
        weight = int(spec["weight"])
        kws = sorted(spec["keywords"], key=len, reverse=True)
        for kw in kws:
            if kw.lower() in lowered:
                hits.append(KeywordMatch(category=category, keyword=kw, weight=weight))
    return hits


def categories_from_matches(matches: list[KeywordMatch]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        if m.category not in seen:
            seen.add(m.category)
            out.append(m.category)
    return out


def dominant_category(matches: list[KeywordMatch]) -> str | None:
    if not matches:
        return None
    best_cat = None
    best_w = -1
    for m in matches:
        if m.weight > best_w:
            best_w = m.weight
            best_cat = m.category
    return best_cat


def keyword_weight_sum_for_categories(
    categories: list[str], weights: dict[str, float] | None = None
) -> float:
    src = weights or {k: float(v["weight"]) for k, v in RISK_KEYWORDS.items()}
    return float(sum(src[c] for c in categories if c in src))


def categories_from_stored_keywords(keywords: list[str] | None) -> list[str]:
    """Infer lexicon categories from stored matched keyword strings (exact or substring)."""
    if not keywords:
        return []
    cats: list[str] = []
    seen: set[str] = set()
    stored_l = [k.lower() for k in keywords]
    for cat, spec in RISK_KEYWORDS.items():
        for kw in spec["keywords"]:
            kwl = kw.lower()
            if any(s == kwl or kwl in s or s in kwl for s in stored_l):
                if cat not in seen:
                    seen.add(cat)
                    cats.append(cat)
                break
    return cats
