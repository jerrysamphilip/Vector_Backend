"""How a field value is written into property history and compared for changes (crm, workflow rules,
outbound webhooks share it). Pure."""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional


def fmt_value(value: Any) -> Optional[str]:
    """The value as stored in property history: None for empty, JSON for lists/dicts, ISO dates."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)
