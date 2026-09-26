from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class Merchant:
    merchant_id: str
    category_slug: str

    # Basic business information
    name: str
    city: str
    locality: str

    # Platform  metadata
    place_id: str = ""
    verified: bool = False
    languages: List[str] = field(default_factory=list)

    # Merchant's actual performance/context
    stats: Dict[str, Any] = field(default_factory=dict)

    # Historical interaction information
    history: List[Dict[str, Any]] = field(default_factory=list)

    # Current active offers
    active_offers: List[Dict[str, Any]] = field(default_factory=list)

    # Additional merchant-specific information
    metadata: Dict[str, Any] = field(default_factory=dict)