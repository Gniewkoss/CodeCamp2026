"""Risk event type → base severity (0.0–1.0)."""

from __future__ import annotations

EVENT_TYPES: dict[str, float] = {
    # Legal / criminal
    "corruption_allegation": 0.9,
    "criminal_charges": 0.95,
    "conviction": 1.0,
    "investigation_opened": 0.75,
    "arrest": 0.9,
    # Regulatory
    "regulatory_fine": 0.6,
    "license_revoked": 0.85,
    "regulatory_warning": 0.4,
    "tax_arrears": 0.5,
    # Sanctions
    "sanctions_match_company": 1.0,
    "sanctions_match_person": 0.85,
    # Financial
    "bankruptcy_filed": 0.9,
    "debt_restructuring": 0.6,
    "payment_backlog": 0.4,
    # Management
    "ceo_resignation_scandal": 0.6,
    "ceo_resignation_normal": 0.1,
    "board_member_arrested": 0.95,
    "key_person_departure": 0.3,
    # Media
    "negative_media_spike": 0.5,
    "reputational_crisis": 0.7,
}

EVENT_TYPE_CHOICES = tuple(sorted(EVENT_TYPES.keys()))
