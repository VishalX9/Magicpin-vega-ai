from dataclasses import dataclass, field
from typing import List, Literal, Optional


@dataclass
class ComposedMessage:
    """
    Output of compose() — brief §5 / §7.1.
    Required keys: body, cta, send_as, suppression_key, rationale.
    """

    body: str
    cta: Literal["yes_stop", "open_ended", "none"]
    send_as: Literal["vera", "merchant_on_behalf"]
    suppression_key: str
    rationale: str

    trigger_id: Optional[str] = None
    customer_id: Optional[str] = None
    template_name: Optional[str] = None
    template_params: List[str] = field(default_factory=list)
