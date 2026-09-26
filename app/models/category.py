from dataclasses import dataclass
from typing import Any


@dataclass
class Category:
    slug: str
    display_name: str

    voice: dict
    offer_catalog: list
    peer_stats: dict
    digest: list
    patient_content_library: list
    seasonal_beats: list
    trend_signals: list
    regulatory_authorities: list
    professional_journals: list