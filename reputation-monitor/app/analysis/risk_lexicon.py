"""Risk lexicon — used as a fast pre-filter hint for the LLM and for
backwards-compatible category weights in the scoring engine.

The LLM does the real analysis; the lexicon just helps the scoring layer
understand category weights when the model emits standardised labels.
"""

from __future__ import annotations

RISK_CATEGORIES: dict[str, dict] = {
    "corruption": {
        "label": "Korupcja",
        "weight": 10.0,
        "keywords": [
            "korupcja", "łapówka", "łapówki", "przekupstwo", "bribery", "corruption",
            "CBA", "ABW", "kickback",
        ],
    },
    "legal": {
        "label": "Sprawy prawne",
        "weight": 8.0,
        "keywords": [
            "zarzuty", "prokuratura", "areszt", "sąd", "wyrok", "pozew",
            "charges", "indicted", "arrested", "lawsuit", "investigation",
        ],
    },
    "management": {
        "label": "Zarząd",
        "weight": 5.5,
        "keywords": [
            "rezygnacja zarządu", "odwołanie", "zwolnienie prezesa",
            "CEO resignation", "fired", "dymisja", "zarząd",
        ],
    },
    "sanctions": {
        "label": "Sankcje",
        "weight": 9.5,
        "keywords": ["sankcje", "sanctions", "OFAC", "blacklist", "czarna lista", "embargo"],
    },
    "financial": {
        "label": "Problemy finansowe",
        "weight": 7.5,
        "keywords": [
            "upadłość", "bankructwo", "windykacja", "niewypłacalność",
            "bankruptcy", "insolvency", "fraud", "oszustwo", "wyłudzenie",
        ],
    },
    "money_laundering": {
        "label": "Pranie pieniędzy",
        "weight": 10.0,
        "keywords": ["pranie pieniędzy", "money laundering", "AML", "KYC"],
    },
    "regulatory": {
        "label": "Regulacyjne",
        "weight": 6.5,
        "keywords": [
            "KNF", "UOKiK", "kara", "naruszenie", "nadzór",
            "investigation", "fine", "penalty", "regulator",
        ],
    },
    "operational": {
        "label": "Operacyjne",
        "weight": 3.5,
        "keywords": ["wyciek danych", "data breach", "cyberattack", "strajk", "strike"],
    },
    "esg": {
        "label": "ESG",
        "weight": 3.0,
        "keywords": ["greenwashing", "pollution", "zanieczyszczenie", "skandal ekologiczny"],
    },
}


def category_weight(category: str) -> float:
    spec = RISK_CATEGORIES.get((category or "").lower())
    return float(spec["weight"]) if spec else 0.0


def category_label(category: str) -> str:
    spec = RISK_CATEGORIES.get((category or "").lower())
    return str(spec["label"]) if spec else category


def quick_keyword_hints(text: str) -> list[str]:
    """Case-insensitive substring scan — used only as a short hint list
    fed to the LLM so it can't miss obvious signals."""
    if not text:
        return []
    lowered = text.lower()
    hits: list[str] = []
    for cat, spec in RISK_CATEGORIES.items():
        for kw in spec["keywords"]:
            if kw.lower() in lowered and kw not in hits:
                hits.append(kw)
    return hits[:40]
