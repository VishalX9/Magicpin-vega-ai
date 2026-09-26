from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Customer:
    customer_id: str
    merchant_id: str

    # Customer identity
    name: str
    phone_redacted: str = ""
    language_pref: str = "en"

    # Relationship with the merchant
    first_visit: Optional[str] = None
    last_visit: Optional[str] = None
    visits_total: int = 0
    services_received: List[str] = field(default_factory=list)

    # Current customer state
    state: str = "unknown"

    # Customer preferences
    preferences: Dict[str, Any] = field(default_factory=dict)

    # Communication consent
    consent: Dict[str, Any] = field(default_factory=dict)