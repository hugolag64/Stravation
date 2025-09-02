# dates.py
from __future__ import annotations
import pendulum as p

def monday_of(date_iso: str, tz: str = "Europe/Paris") -> p.DateTime:
    d = p.parse(date_iso).in_timezone(tz)
    return d.start_of("week")

def iso_local(dt: p.DateTime) -> str:
    return dt.to_datetime_string()

# idempotency.py
from __future__ import annotations
import hashlib

def stable_id(*parts: str, prefix: str = "sn") -> str:
    raw = "::".join(parts).encode()
    return f"{prefix}-{hashlib.sha1(raw).hexdigest()}"
